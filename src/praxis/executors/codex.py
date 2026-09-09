"""Codex CLI integration; native options never escape the codex namespace.

Interface: https://learn.chatgpt.com/docs/non-interactive-mode
The host must provision Codex credentials in the explicit process environment.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

from praxis.executors.features import ExecutorFeatures
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.events import Event
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.workspaces.local import LocalWorkspaces


class CodexExecutor(LocalProcessExecutor):
    def __init__(self, workspaces: LocalWorkspaces, authority: Authority,
                 executable: str = "codex", event_sink: Callable[[Event], None] | None = None):
        super().__init__(workspaces)
        self.authority = authority
        self.executable = executable
        self.events: list[Event] = []
        self.event_sink = event_sink

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("codex", frozenset({"cancel", "streaming"}))

    def map_request(self, request: ExecutionRequest) -> ExecutionRequest:
        options = request.spec.metadata.get("codex", {})
        if not isinstance(options, dict) or set(options) - {"model"}:
            raise ValueError("invalid_codex_options")
        model = options.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()
                                  or "\x00" in model):
            raise ValueError("invalid_codex_model")
        writable = self.authority.authorize(
            request.process_id, Resource.FILESYSTEM, "write", str(request.workspace_path)
        ).allowed
        argv = [self.executable, "exec", "--json", "--cd", str(request.workspace_path),
                "--sandbox", "workspace-write" if writable else "read-only",
                "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "-c", "approval_policy=\"never\"",
                "-c", "sandbox_workspace_write.network_access=false"]
        if model is not None:
            argv.extend(["--model", model])
        argv.append("-")
        prompt = json.dumps({"objective": request.spec.objective, "inputs": request.spec.inputs},
                            ensure_ascii=False, allow_nan=False)
        return replace(request, spec=replace(request.spec, inputs={"argv": argv, "stdin": prompt}))

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if not self.authority.authorize(
            request.process_id, Resource.EXECUTOR, "execute", "codex"
        ).allowed:
            return ControlResult(True, False, "codex_capability_denied")
        try:
            mapped = self.map_request(request)
        except ValueError:
            return ControlResult(True, False, "invalid_codex_options")
        return await super().start(mapped)

    async def _collect(
        self, attempt_id: str, process: asyncio.subprocess.Process,
        stdin: bytes, timeout: float | None,
    ) -> Outcome:
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        request = self.requests[attempt_id]
        stderr_task = asyncio.create_task(process.stderr.read())
        messages: list[str] = []
        terminal: str | None = None
        malformed = False
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
                        raise ValueError("invalid event")
                    event = Event(request.process_id, "executor.stream", {
                        "attempt_id": attempt_id, "executor": "codex", "codex": raw})
                    event.to_json()
                    self.events.append(event)
                    if self.event_sink is not None:
                        self.event_sink(event)
                    if raw["type"] in {"turn.completed", "turn.failed", "error"}:
                        terminal = raw["type"]
                    item = raw.get("item", {})
                    if raw["type"] == "item.completed" and isinstance(item, dict):
                        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                            messages.append(item["text"])
                except (ValueError, TypeError):
                    malformed = True
            await process.wait()
        except (OSError, ValueError):
            malformed = True
            self._kill(process)
            await process.wait()
        stderr = (await stderr_task).decode(errors="replace")
        if attempt_id in self.cancelled:
            status, reason = OutcomeStatus.CANCELLED, "cancelled"
        elif malformed:
            status, reason = OutcomeStatus.PARTIAL, "codex_invalid_stream"
        elif terminal in {"turn.failed", "error"}:
            status, reason = OutcomeStatus.FAILED, "codex_turn_failed"
        elif terminal == "turn.completed" and process.returncode == 0:
            status, reason = OutcomeStatus.COMPLETED, "codex_completed"
        elif self.events and any(e.payload["attempt_id"] == attempt_id for e in self.events):
            status, reason = OutcomeStatus.PARTIAL, "codex_incomplete_stream"
        else:
            status, reason = OutcomeStatus.UNAVAILABLE, "codex_unavailable"
        outcome = Outcome(status, reason, "\n".join(messages), stderr, process.returncode)
        self.results[attempt_id] = outcome
        return outcome
