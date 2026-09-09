"""Optional Claude Agent SDK adapter using the stateless query interface.

https://code.claude.com/docs/en/agent-sdk/python
Tool-free by default: allowed_tools alone is not a security restriction.
"""

import asyncio
import importlib
import json
from typing import Any

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


class ClaudeExecutor(FakeExecutor):
    def __init__(self, workspaces: LocalWorkspaces, authority: Authority, sdk: Any = None):
        super().__init__()
        self.workspaces = workspaces
        self.authority = authority
        self.sdk = sdk
        self.tasks: dict[str, asyncio.Task[Outcome]] = {}

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("claude", frozenset())

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if request.attempt_id in self.requests:
            same = self.requests[request.attempt_id] == request
            return ControlResult(True, same, "duplicate" if same else "identity_conflict")
        if not self.authority.authorize(request.process_id, Resource.EXECUTOR,
                                        "execute", "claude").allowed:
            return ControlResult(True, False, "claude_capability_denied")
        try:
            handle = WorkspaceHandle(request.workspace_id, request.process_id, "local")
            path = self.workspaces.path_for(handle, request.process_id)
            if request.workspace_path.resolve() != path:
                raise ValueError("workspace mismatch")
            native = request.spec.metadata.get("claude", {})
            if not isinstance(native, dict) or set(native) - {"model"}:
                raise ValueError("invalid options")
            if "model" in native and (not isinstance(native["model"], str) or not native["model"]):
                raise ValueError("invalid model")
            sdk = self.sdk or importlib.import_module("claude_agent_sdk")
            options = sdk.ClaudeAgentOptions(
                cwd=str(path), tools=[], setting_sources=[], strict_mcp_config=True,
                mcp_servers={}, permission_mode="dontAsk", env=request.spec.environment,
                **native)
        except (ImportError, OSError, ValueError, TypeError):
            return ControlResult(True, False, "claude_unavailable")
        self.requests[request.attempt_id] = request
        self.tasks[request.attempt_id] = asyncio.create_task(self._run(request, sdk, options))
        return ControlResult(True, True, "started")

    async def _run(self, request: ExecutionRequest, sdk: Any, options: Any) -> Outcome:
        outcome = Outcome(OutcomeStatus.PARTIAL, "claude_missing_result")
        try:
            prompt = json.dumps({"objective": request.spec.objective, "inputs": request.spec.inputs})
            async for message in sdk.query(prompt=prompt, options=options):
                if isinstance(message, sdk.ResultMessage):
                    status = (OutcomeStatus.FAILED if message.is_error
                              else OutcomeStatus.COMPLETED if message.subtype == "success"
                              else OutcomeStatus.PARTIAL)
                    outcome = Outcome(status, "claude_" + message.subtype, message.result or "")
        except Exception:
            outcome = Outcome(OutcomeStatus.UNAVAILABLE, "claude_transport_error",
                              stdout=outcome.stdout, retryable=True)
        self.results[request.attempt_id] = outcome
        return outcome

    async def collect_result(self, attempt_id: str) -> Outcome:
        if attempt_id not in self.tasks:
            return Outcome(OutcomeStatus.UNAVAILABLE, "attempt_not_found")
        return await asyncio.shield(self.tasks[attempt_id])

    async def cancel(self, attempt_id: str) -> ControlResult:
        return ControlResult(False, False, "cancel_unavailable")
