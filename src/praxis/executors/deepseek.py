"""Optional DeepSeek Harness SDK integration for host-isolated workers.

The sdk-minimal upstream profile permits unrestricted native tools. The host
must isolate the entire worker, including its environment, before enabling it.
https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk
"""

import asyncio
import importlib
import json
import tempfile
from pathlib import Path
from typing import Any

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


class DeepSeekExecutor(FakeExecutor):
    def __init__(self, workspaces: LocalWorkspaces, authority: Authority, home_root: Path,
                 *, isolated_worker: bool = False, sdk: Any = None):
        super().__init__()
        self.workspaces = workspaces
        self.authority = authority
        self.home_root = home_root.resolve()
        self.isolated_worker = isolated_worker
        self.sdk = sdk
        self.tasks: dict[str, asyncio.Task[Outcome]] = {}

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("deepseek", frozenset())

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if request.attempt_id in self.requests:
            same = self.requests[request.attempt_id] == request
            return ControlResult(True, same, "duplicate" if same else "identity_conflict")
        if not self.isolated_worker:
            return ControlResult(True, False, "isolation_required")
        if not self.authority.authorize(request.process_id, Resource.EXECUTOR,
                                        "execute", "deepseek").allowed:
            return ControlResult(True, False, "deepseek_capability_denied")
        try:
            handle = WorkspaceHandle(request.workspace_id, request.process_id, "local")
            path = self.workspaces.path_for(handle, request.process_id)
            if request.workspace_path.resolve() != path:
                raise ValueError("workspace mismatch")
            native = request.spec.metadata.get("deepseek", {})
            if not isinstance(native, dict) or set(native) - {"model", "provider"}:
                raise ValueError("invalid options")
            if any(not isinstance(value, str) or not value for value in native.values()):
                raise ValueError("invalid options")
            sdk = self.sdk or importlib.import_module("deepseek_harness")
            self.home_root.mkdir(parents=True, exist_ok=True)
        except (ImportError, OSError, ValueError, TypeError):
            return ControlResult(True, False, "deepseek_unavailable")
        self.requests[request.attempt_id] = request
        self.tasks[request.attempt_id] = asyncio.create_task(
            asyncio.to_thread(self._run, request, sdk, native))
        return ControlResult(True, True, "started")

    def _run(self, request: ExecutionRequest, sdk: Any, native: dict[str, Any]) -> Outcome:
        try:
            with tempfile.TemporaryDirectory(dir=self.home_root) as home:
                with sdk.DeepSeekHarness(cwd=str(request.workspace_path),
                                         runtime_cwd=str(request.workspace_path),
                                         dsh_home=home, profile="sdk-minimal",
                                         env=request.spec.environment, **native) as harness:
                    result = harness.run(json.dumps({"objective": request.spec.objective,
                                                     "inputs": request.spec.inputs}),
                                         session_id=request.attempt_id)
                status = {"completed": OutcomeStatus.COMPLETED, "error": OutcomeStatus.FAILED}.get(
                    result.finish_reason, OutcomeStatus.PARTIAL)
                outcome = Outcome(status, "deepseek_" + (result.finish_reason or "incomplete"),
                                  result.final_response)
        except Exception:
            outcome = Outcome(OutcomeStatus.UNAVAILABLE, "deepseek_transport_error", retryable=True)
        self.results[request.attempt_id] = outcome
        return outcome

    async def collect_result(self, attempt_id: str) -> Outcome:
        if attempt_id not in self.tasks:
            return Outcome(OutcomeStatus.UNAVAILABLE, "attempt_not_found")
        return await asyncio.shield(self.tasks[attempt_id])

    async def cancel(self, attempt_id: str) -> ControlResult:
        return ControlResult(False, False, "cancel_unavailable")
