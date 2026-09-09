"""One protocol contract exercised against every shipped adapter without credentials."""
import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from praxis.executors.claude import ClaudeExecutor
from praxis.executors.codex import CodexExecutor
from praxis.executors.deepseek import DeepSeekExecutor
from praxis.executors.fake import FakeExecutor
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest, Executor
from praxis.executors.shell import ShellExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("name", ["fake", "local", "shell", "codex", "claude", "deepseek"])
def test_adapter_conformance(tmp_path, name):
    class Result(SimpleNamespace):
        pass
    async def query(**kwargs):
        yield Result(subtype="success", is_error=False, result="done")
    class Harness:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, *args, **kwargs):
            return Result(finish_reason="completed", final_response="done")
    async def exercise():
        provider = LocalWorkspaces(tmp_path / "ws")
        authority = Authority()
        authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), name)
        authority.issue("p", Resource.EXECUTOR, frozenset({"shell"}), "shell")
        executable = tmp_path / "fixture"
        executable.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"type":"turn.completed"}\\n\'\n')
        executable.chmod(0o700)
        adapters = {
            "fake": FakeExecutor(), "local": LocalProcessExecutor(provider),
            "shell": ShellExecutor(provider, authority),
            "codex": CodexExecutor(provider, authority, str(executable)),
            "claude": ClaudeExecutor(provider, authority, SimpleNamespace(
                query=query, ResultMessage=Result, ClaudeAgentOptions=lambda **kw: kw)),
            "deepseek": DeepSeekExecutor(provider, authority, tmp_path / "homes",
                                         isolated_worker=True, sdk=SimpleNamespace(DeepSeekHarness=Harness)),
        }
        adapter = adapters[name]
        assert isinstance(adapter, Executor)
        handle = provider.create("p")
        inputs = {"argv": [sys.executable, "-c", "pass"]} if name == "local" else (
            {"command": "true"} if name == "shell" else {})
        request = ExecutionRequest("p", "a", ProcessSpec("task", name, inputs=inputs),
                                   handle.workspace_id, provider.path_for(handle, "p"))
        assert (await adapter.collect_result("unknown")).status == OutcomeStatus.UNAVAILABLE
        assert (await adapter.start(request)).applied
        assert (await adapter.start(request)).reason == "duplicate"
        assert not (await adapter.start(replace(request, spec=replace(request.spec, objective="different")))).applied
        result = await adapter.collect_result("a")
        assert result.status == OutcomeStatus.COMPLETED
        assert await adapter.collect_result("a") == result
        assert not (await adapter.cancel("a")).applied
        assert not (await adapter.checkpoint("a")).control.supported
    asyncio.run(exercise())


@pytest.mark.parametrize("transport_error", [False, True])
def test_claude_to_codex_fallback(tmp_path, transport_error):
    from praxis.kernel.process import ProcessRecords
    from praxis.kernel.runtime import Kernel
    from praxis.kernel.scheduler import PlacementPolicy, Scheduler

    async def query(**kwargs):
        if transport_error:
            raise OSError("offline")
        if False:
            yield None
    async def exercise():
        provider = LocalWorkspaces(tmp_path / "ws")
        authority = Authority(execution_defaults=frozenset({"claude", "codex"}))
        executable = tmp_path / "codex"
        executable.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"type":"turn.completed"}\\n\'\n')
        executable.chmod(0o700)
        sdk = SimpleNamespace(query=query, ResultMessage=SimpleNamespace,
                              ClaudeAgentOptions=lambda **kw: kw)
        # Missing optional SDK and transport interruption are separate unavailable paths.
        if not transport_error:
            def unavailable(**kwargs):
                raise ImportError("missing optional dependency")
            sdk.ClaudeAgentOptions = unavailable
        kernel = Kernel(ProcessRecords(tmp_path / "records"), provider, {
            "claude": ClaudeExecutor(provider, authority, sdk),
            "codex": CodexExecutor(provider, authority, str(executable)),
        }, authority=authority)
        process = kernel.create(ProcessSpec("task", "claude", metadata={"lineage_label": "original"}))
        original = process.spec.to_json()
        grants = dict(authority.grants)
        scheduler = Scheduler(kernel)
        scheduler.enqueue(process.process_id, PlacementPolicy(("claude", "codex")))
        assert (await scheduler.drain())[0].status == OutcomeStatus.COMPLETED
        assert process.spec.to_json() == original
        assert all(authority.grants[k] == v for k, v in grants.items())
        assert len({h.attempt_id for h in process.history}) == 2
    asyncio.run(exercise())
