"""Codex, Claude and DeepSeek against their live services.

Each adapter needs its credential, its CLI or SDK, and PRAXIS_ACCEPTANCE_ISOLATED_WORKER=1:
the operator's assertion that this runner is a disposable, independently confined worker.
The adapters' ``isolated_worker=True`` flag creates no sandbox (docs/isolation.md).
"""

import asyncio
import importlib
import importlib.metadata
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from praxis.executors.protocol import Executor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.lifecycle import State
from praxis.kernel.runtime import Kernel
from praxis.kernel.secrets import SecretAccess
from praxis.kernel.spec import ProcessSpec
from praxis.observability.redaction import RedactionPolicy
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory

WRITE_FILE = ("Create a file named answer.txt in the current directory whose entire content is the "
              "single word accepted. Do not create or change any other file.")
NO_FILES = "Reply with the single word ok. Do not create or change any file."
SLOW = ("Write the numbers from 1 to 200000 to a file named count.txt, one per line, one number at a "
        "time, and reply done when finished.")


def isolated() -> None:
    if os.environ.get("PRAXIS_ACCEPTANCE_ISOLATED_WORKER") != "1":
        pytest.skip("PRAXIS_ACCEPTANCE_ISOLATED_WORKER=1 not set: runner not asserted as a confined worker")


def need(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"{variable} not set")
    return value


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class EnvironmentVault:
    """Resolves bound secret names from this runner's environment, as a host vault would."""

    def __init__(self, names: dict[str, str]):
        self.names = names

    def resolve(self, name: str) -> str:
        return os.environ[self.names[name]]


def boot(root: Path, name: str, make: Callable[[LocalWorkspaces, Authority], Executor]):
    store = SQLiteStore(root / "runtime.db")
    workspaces = LocalWorkspaces(root / "workspaces")
    authority = Authority(execution_defaults=frozenset({name}))
    kernel = Kernel(store, workspaces, {name: make(workspaces, authority)}, authority=authority)
    return kernel, store, CanonicalDirectory(root / "canonical")


async def run(kernel: Kernel, canonical: CanonicalDirectory, spec: ProcessSpec, *, cancel_after: float | None = None):
    process = kernel.create(spec, canonical=canonical)
    kernel.authority.issue(process.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
    # Codex runs in workspace-write mode only when the process may write its workspace.
    kernel.authority.issue(process.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}),
                           str(kernel.workspaces.data))
    kernel.authority.issue(process.process_id, Resource.WORKSPACE, frozenset({"commit"}), process.process_id)
    for name in getattr(kernel.executors[spec.executor], "secret_bindings", {}).values():
        kernel.authority.issue(process.process_id, Resource.SECRET, frozenset({"read"}), name)
    kernel.start(process.process_id)
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        await kernel.cancel(process.process_id)
    await kernel.tasks[process.process_id]
    return kernel.result(process.process_id)


def spec(executor: str, objective: str, outputs: list[str], model_variable: str) -> ProcessSpec:
    native = {"model": os.environ[model_variable]} if os.environ.get(model_variable) else {}
    return ProcessSpec(objective, executor, contract={"required_outputs": outputs},
                       metadata={executor: native} if native else {})


def assert_contract_failure_publishes_nothing(result, canonical: CanonicalDirectory) -> None:
    assert result.state == State.FAILED
    assert result.verification is not None and result.verification.approved is False
    assert result.verification.missing_outputs == ("never-created.txt",)
    assert not any(canonical.path.iterdir())


# Codex --------------------------------------------------------------------------------------------

def codex(root: Path):
    isolated()
    need("CODEX_API_KEY")
    executable = os.environ.get("PRAXIS_ACCEPTANCE_CODEX_BIN", "codex")
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} CLI not found")
    from praxis.executors.codex import CodexExecutor

    def make(workspaces: LocalWorkspaces, authority: Authority) -> Executor:
        access = SecretAccess(EnvironmentVault({"codex/api-key": "CODEX_API_KEY"}), authority, RedactionPolicy())
        return CodexExecutor(workspaces, authority, executable, isolated_worker=True, secrets=access,
                             secret_bindings={"CODEX_API_KEY": "codex/api-key"})
    return (*boot(root, "codex", make), executable)


def test_codex_verified_success(scanned, versions):
    kernel, store, canonical, executable = codex(scanned)
    versions["codex"] = subprocess.run([executable, "--version"], capture_output=True, text=True).stdout.strip()
    result = asyncio.run(run(kernel, canonical, spec("codex", WRITE_FILE, ["answer.txt"], "PRAXIS_ACCEPTANCE_CODEX_MODEL")))
    assert result.state == State.COMPLETED and result.verification.approved
    assert (canonical.path / "answer.txt").read_text().strip() == "accepted"
    store.close()


def test_codex_contract_failure(scanned):
    kernel, store, canonical, _ = codex(scanned)
    result = asyncio.run(run(kernel, canonical, spec("codex", NO_FILES, ["never-created.txt"], "PRAXIS_ACCEPTANCE_CODEX_MODEL")))
    assert_contract_failure_publishes_nothing(result, canonical)
    store.close()


def test_codex_cancellation(scanned):
    kernel, store, canonical, _ = codex(scanned)
    delay = float(os.environ.get("PRAXIS_ACCEPTANCE_CANCEL_AFTER", "5"))
    result = asyncio.run(run(kernel, canonical, spec("codex", SLOW, ["count.txt"], "PRAXIS_ACCEPTANCE_CODEX_MODEL"),
                             cancel_after=delay))
    assert result.state == State.CANCELLED and result.outcome.reason == "cancelled"
    assert not any(canonical.path.iterdir())
    store.close()


# Claude (tool-free: it can answer but cannot write files) ------------------------------------------

def claude(root: Path):
    isolated()
    need("ANTHROPIC_API_KEY")
    try:
        importlib.import_module("claude_agent_sdk")
    except ImportError:
        pytest.skip("claude-agent-sdk not installed")
    from praxis.executors.claude import ClaudeExecutor
    return boot(root, "claude", lambda w, a: ClaudeExecutor(w, a, isolated_worker=True))


def test_claude_verified_success(scanned, versions):
    kernel, store, canonical = claude(scanned)
    versions["claude-agent-sdk"] = package_version("claude-agent-sdk")
    result = asyncio.run(run(kernel, canonical, spec("claude", NO_FILES, [], "PRAXIS_ACCEPTANCE_CLAUDE_MODEL")))
    assert result.state == State.COMPLETED and result.verification.approved
    assert "ok" in result.outcome.stdout.lower()
    store.close()


def test_claude_contract_failure(scanned):
    kernel, store, canonical = claude(scanned)
    result = asyncio.run(run(kernel, canonical, spec("claude", NO_FILES, ["never-created.txt"], "PRAXIS_ACCEPTANCE_CLAUDE_MODEL")))
    assert_contract_failure_publishes_nothing(result, canonical)
    store.close()


# DeepSeek ------------------------------------------------------------------------------------------

def deepseek(root: Path):
    isolated()
    need("DEEPSEEK_API_KEY")
    try:
        importlib.import_module("deepseek_harness")
    except ImportError:
        pytest.skip("deepseek-harness not installed")
    from praxis.executors.deepseek import DeepSeekExecutor
    return boot(root, "deepseek", lambda w, a: DeepSeekExecutor(w, a, root / "deepseek-home", isolated_worker=True))


def test_deepseek_verified_success(scanned, versions):
    kernel, store, canonical = deepseek(scanned)
    versions["deepseek-harness"] = package_version("deepseek-harness")
    result = asyncio.run(run(kernel, canonical, spec("deepseek", WRITE_FILE, ["answer.txt"], "PRAXIS_ACCEPTANCE_DEEPSEEK_MODEL")))
    assert result.state == State.COMPLETED and result.verification.approved
    assert (canonical.path / "answer.txt").read_text().strip() == "accepted"
    store.close()


def test_deepseek_contract_failure(scanned):
    kernel, store, canonical = deepseek(scanned)
    result = asyncio.run(run(kernel, canonical, spec("deepseek", NO_FILES, ["never-created.txt"], "PRAXIS_ACCEPTANCE_DEEPSEEK_MODEL")))
    assert_contract_failure_publishes_nothing(result, canonical)
    store.close()
