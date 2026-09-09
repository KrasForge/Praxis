"""Kernel-side remote executor; transport success never substitutes for an outcome."""

import asyncio
import json
from collections.abc import Callable

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.events import Event
from praxis.kernel.lineage import Lineage
from praxis.remote.bundle import WorkspaceBundle
from praxis.remote.dispatch import Dispatch
from praxis.remote.workers import Worker
from praxis.transport.http import JSONTransport, TransportError
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


class RemoteExecutor(FakeExecutor):
    def __init__(self, worker: Worker, executor: str, workspaces: LocalWorkspaces,
                 transport: JSONTransport, event_sink: Callable[[Event], None] | None = None,
                 features: frozenset[str] = frozenset()):
        super().__init__()
        self.control_features = features & {"cancel"}
        self.worker = worker
        self.executor = executor
        self.workspaces = workspaces
        self.transport = transport
        self.event_sink = event_sink
        self.dispatches: dict[str, Dispatch] = {}
        self.cursors: dict[str, int] = {}
        self.events: list[Event] = []
        self.result_locks: dict[str, asyncio.Lock] = {}

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("remote", frozenset({"streaming"}) | self.control_features)

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if request.attempt_id in self.requests:
            if self.requests[request.attempt_id] != request:
                return ControlResult(True, False, "identity_conflict")
            return await self.reconnect(request.attempt_id)
        handle = WorkspaceHandle(request.workspace_id, request.process_id, "local")
        if self.workspaces.path_for(handle, request.process_id) != request.workspace_path.resolve():
            return ControlResult(True, False, "workspace_mismatch")
        dispatch = Dispatch(self.worker.worker_id, self.worker.generation, request.process_id,
                            request.attempt_id, self.executor, request.spec.to_json(),
                            WorkspaceBundle.capture(self.workspaces, handle).to_json(),
                            request.lineage_json or Lineage(request.process_id, request.attempt_id).to_json(), request.parent_id)
        self.requests[request.attempt_id] = request
        self.dispatches[request.attempt_id] = dispatch
        event = Event(request.process_id, "worker.dispatch_pending", {"attempt_id": request.attempt_id,
                      "worker_id": self.worker.worker_id, "execution_id": dispatch.execution_id,
                      "lineage": dispatch.lineage_json}, parent_id=request.parent_id)
        self.events.append(event)
        if self.event_sink:
            self.event_sink(event)
        return await self.reconnect(request.attempt_id)

    async def reconnect(self, attempt_id: str) -> ControlResult:
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            return ControlResult(True, False, "attempt_not_found")
        try:
            reply = await self.transport.request("POST", "/v1/worker", {"operation": "dispatch", "dispatch": json.loads(dispatch.to_json())})
            if reply.get("accepted") is not True or reply.get("execution_id") != dispatch.execution_id:
                return ControlResult(True, False, "remote_dispatch_rejected")
        except TransportError:
            # The worker may have accepted the dispatch before the acknowledgement was lost.
            return ControlResult(True, True, "remote_dispatch_uncertain")
        return ControlResult(True, True, "duplicate" if reply.get("duplicate") else "started")

    async def collect_result(self, attempt_id: str) -> Outcome:
        async with self.result_locks.setdefault(attempt_id, asyncio.Lock()):
            return await self._collect_result(attempt_id)

    async def _collect_result(self, attempt_id: str) -> Outcome:
        if attempt_id in self.results:
            return self.results[attempt_id]
        if attempt_id not in self.dispatches:
            return Outcome(OutcomeStatus.UNAVAILABLE, "attempt_not_found")
        dispatch = self.dispatches[attempt_id]
        try:
            while True:
                reply = await self.transport.request("POST", "/v1/worker", {
                    "operation": "poll", "execution_id": dispatch.execution_id, "after": self.cursors.get(attempt_id, 0)})
                if (reply.get("execution_id"), reply.get("process_id"), reply.get("attempt_id"), reply.get("generation")) != (
                    dispatch.execution_id, dispatch.process_id, attempt_id, self.worker.generation):
                    raise ValueError("remote_result_identity_mismatch")
                for entry in reply["events"]:
                    event = Event.from_json(json.dumps(entry["event"]))
                    if event.process_id != dispatch.process_id or event.parent_id != dispatch.parent_id or event.payload.get("attempt_id") != attempt_id:
                        raise ValueError("remote_event_identity_mismatch")
                    if event.type not in {"worker.execution_started", "worker.execution_completed"} or event.payload.get("worker_id") != self.worker.worker_id or event.payload.get("generation") != self.worker.generation or event.payload.get("lineage") != dispatch.lineage_json:
                        raise ValueError("remote_event_provenance_mismatch")
                    if type(entry["cursor"]) is not int or entry["cursor"] <= self.cursors.get(attempt_id, 0):
                        raise ValueError("remote_event_cursor_replay")
                    self.events.append(event)
                    if self.event_sink:
                        self.event_sink(event)
                    self.cursors[attempt_id] = entry["cursor"]
                if reply.get("orphaned") is True:
                    return Outcome(OutcomeStatus.PARTIAL, "remote_execution_orphaned")
                if reply["result"] is not None:
                    result = reply["result"]
                    outcome = Outcome.from_json(json.dumps(result["outcome"]))
                    if result["workspace"] is not None:
                        request = self.requests[attempt_id]
                        WorkspaceBundle.from_json(json.dumps(result["workspace"])).restore(self.workspaces,
                            WorkspaceHandle(request.workspace_id, request.process_id, "local"))
                    self.results[attempt_id] = outcome
                    return outcome
                await asyncio.sleep(0.05)
        except TransportError:
            return Outcome(OutcomeStatus.PARTIAL, "remote_transport_unavailable")
        except (ValueError, TypeError, KeyError):
            return Outcome(OutcomeStatus.FAILED, "remote_protocol_error")

    async def cancel(self, attempt_id: str) -> ControlResult:
        if "cancel" not in self.control_features:
            return ControlResult(False, False, "cancel_unavailable")
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            return ControlResult(True, False, "attempt_not_found")
        try:
            reply = await self.transport.request("POST", "/v1/worker", {
                "operation": "cancel", "execution_id": dispatch.execution_id,
                "attempt_id": attempt_id, "generation": self.worker.generation})
            if any(type(reply.get(k)) is not bool for k in ("supported", "applied")):
                raise ValueError("invalid remote control")
            control = ControlResult(**reply)
            if control.applied:
                await self.collect_result(attempt_id)
            return control
        except TransportError:
            return ControlResult(True, False, "remote_transport_unavailable")
        except (ValueError, TypeError):
            return ControlResult(True, False, "remote_protocol_error")
