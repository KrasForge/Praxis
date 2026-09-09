"""Direct argv execution in an explicitly granted local workspace."""

import asyncio
import math
import os
import signal

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError, WorkspaceHandle


class LocalProcessExecutor(FakeExecutor):
    def __init__(self, workspaces: LocalWorkspaces):
        super().__init__()
        self.workspaces = workspaces
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.tasks: dict[str, asyncio.Task[Outcome]] = {}
        self.cancelled: set[str] = set()
        self.lock = asyncio.Lock()

    @property
    def descriptor(self) -> ExecutorFeatures:
        features = {"cancel"}
        if os.name == "posix":
            features.update({"signal", "suspend"})
        return ExecutorFeatures("local", frozenset(features))

    async def start(self, request: ExecutionRequest) -> ControlResult:
        async with self.lock:
            if request.attempt_id in self.requests:
                same = self.requests[request.attempt_id] == request
                return ControlResult(True, same, "duplicate" if same else "identity_conflict")
            try:
                handle = WorkspaceHandle(request.workspace_id, request.process_id, "local")
                path = self.workspaces.path_for(handle, request.process_id)
                if request.workspace_path.resolve() != path:
                    raise WorkspaceError("workspace grant mismatch")
                argv = request.spec.inputs.get("argv")
                if not isinstance(argv, list) or not argv or any(
                    not isinstance(arg, str) or "\x00" in arg for arg in argv
                ):
                    return ControlResult(True, False, "invalid_argv")
                stdin = request.spec.inputs.get("stdin", "")
                timeout = request.spec.inputs.get("timeout")
                if not isinstance(stdin, str):
                    return ControlResult(True, False, "invalid_stdin")
                if timeout is not None and (
                    type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0
                ):
                    return ControlResult(True, False, "invalid_timeout")
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=path, env=request.spec.environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
                )
            except (OSError, ValueError):
                return ControlResult(True, False, "executor_unavailable")
            self.requests[request.attempt_id] = request
            self.processes[request.attempt_id] = process
            self.tasks[request.attempt_id] = asyncio.create_task(
                self._collect(request.attempt_id, process, stdin.encode(), timeout)
            )
            return ControlResult(True, True, "started")

    def _kill(self, process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    async def _collect(
        self, attempt_id: str, process: asyncio.subprocess.Process,
        stdin: bytes, timeout: float | None,
    ) -> Outcome:
        communication = asyncio.create_task(process.communicate(stdin))
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
        except TimeoutError:
            timed_out = True
            self._kill(process)
            stdout, stderr = await communication
        if attempt_id in self.cancelled:
            status, reason = OutcomeStatus.CANCELLED, "cancelled"
        elif timed_out:
            status, reason = OutcomeStatus.TIMED_OUT, "deadline_exceeded"
        elif process.returncode == 0:
            status, reason = OutcomeStatus.COMPLETED, "exit_zero"
        else:
            status = OutcomeStatus.FAILED
            reason = "signal_terminated" if (process.returncode or 0) < 0 else "exit_nonzero"
        result = Outcome(status, reason, stdout.decode(errors="replace"),
                         stderr.decode(errors="replace"), process.returncode)
        self.results[attempt_id] = result
        return result

    async def collect_result(self, attempt_id: str) -> Outcome:
        if attempt_id not in self.tasks:
            return Outcome(OutcomeStatus.UNAVAILABLE, "attempt_not_found")
        return await asyncio.shield(self.tasks[attempt_id])

    async def cancel(self, attempt_id: str) -> ControlResult:
        process = self.processes.get(attempt_id)
        if process is None:
            return ControlResult(True, False, "attempt_not_found")
        if process.returncode is not None:
            return ControlResult(True, False, "already_terminal")
        self.cancelled.add(attempt_id)
        self._kill(process)
        await self.collect_result(attempt_id)
        return ControlResult(True, True, "cancelled")

    async def signal(self, attempt_id: str, signal_name: str) -> ControlResult:
        if os.name != "posix":
            return ControlResult(False, False, "signal_unavailable")
        signals = {"suspend": signal.SIGSTOP, "resume": signal.SIGCONT,
                   "terminate": signal.SIGTERM, "interrupt": signal.SIGINT}
        if signal_name not in signals:
            return ControlResult(False, False, "signal_unavailable")
        process = self.processes.get(attempt_id)
        if process is None or process.returncode is not None:
            return ControlResult(True, False, "attempt_not_running")
        try:
            os.killpg(process.pid, signals[signal_name])
        except ProcessLookupError:
            return ControlResult(True, False, "already_terminal")
        return ControlResult(True, True, "signal_delivered")
