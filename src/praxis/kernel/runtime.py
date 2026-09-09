"""Process-tree orchestration across pluggable executors."""

import asyncio
from dataclasses import dataclass

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ExecutionRequest, Executor
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.process import Process, ProcessRecords
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


@dataclass(frozen=True)
class ChildOutcome:
    process_id: str
    state: State
    outcome: Outcome


class Kernel:
    def __init__(
        self, records: ProcessRecords, workspaces: LocalWorkspaces,
        executors: dict[str, Executor], *, retain_workspaces: bool = False,
    ):
        self.records = records
        self.workspaces = workspaces
        self.executors = dict(executors)
        self.retain_workspaces = retain_workspaces
        self.processes: dict[str, Process] = {}
        self.tasks: dict[str, asyncio.Task[Outcome]] = {}
        self.results: dict[str, Outcome] = {}
        self.handles: dict[str, WorkspaceHandle] = {}
        self.events: list[Event] = []

    def create(self, spec: ProcessSpec, parent_id: str | None = None) -> Process:
        if parent_id is not None:
            parent = self.processes[parent_id]
            if parent.state in TERMINAL:
                raise ValueError("terminal parent cannot spawn")
        # Authority must be issued by the kernel, never inherited from a spec.
        if spec.capabilities:
            raise ValueError("capability issuance is not configured")
        process = Process(ProcessSpec.from_json(spec.to_json()), parent_id=parent_id)
        self.records.save(process)
        self.processes[process.process_id] = process
        self.events.append(Event(process.process_id, "process.created", parent_id=parent_id))
        return process

    def start(self, process_id: str) -> None:
        process = self.processes[process_id]
        if process.state != State.PENDING or process_id in self.tasks:
            raise ValueError("process already started")
        self.tasks[process_id] = asyncio.create_task(self._run(process))

    def spawn(self, parent_id: str, spec: ProcessSpec) -> str:
        child = self.create(spec, parent_id)
        self.start(child.process_id)
        return child.process_id

    def _move(self, process: Process, state: State) -> None:
        process.move(state)
        self.records.save(process)
        self.events.append(Event(process.process_id, "process.state", {"state": state.value},
                                 parent_id=process.parent_id))

    async def _run(self, process: Process) -> Outcome:
        handle: WorkspaceHandle | None = None
        try:
            executor = self.executors.get(process.spec.executor)
            if executor is None:
                result = Outcome(OutcomeStatus.UNAVAILABLE, "executor_not_found")
            else:
                handle = self.workspaces.create(process.process_id, retain=self.retain_workspaces)
                self.handles[process.process_id] = handle
                request = ExecutionRequest(
                    process.process_id, process.attempt_id, process.spec, handle.workspace_id,
                    self.workspaces.path_for(handle, process.process_id),
                )
                self._move(process, State.RUNNING)
                control = await executor.start(request)
                if not control.applied:
                    result = Outcome(OutcomeStatus.UNAVAILABLE, control.reason)
                else:
                    result = await executor.collect_result(process.attempt_id)
        except Exception:
            result = Outcome(OutcomeStatus.FAILED, "executor_error")
        self.results[process.process_id] = result
        # Nonempty contracts remain unverified until a validator is configured.
        self._move(process, result.process_state(verified=not process.spec.contract))
        if handle is not None:
            self.workspaces.cleanup(handle)
        return result

    async def join(self, parent_id: str, child_ids: list[str]) -> tuple[ChildOutcome, ...]:
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("duplicate child identity")
        for child_id in child_ids:
            child = self.processes[child_id]
            if child.parent_id != parent_id:
                raise ValueError("join requires direct child")
            if child_id not in self.tasks:
                raise ValueError("child has not started")
        results = await asyncio.gather(*(asyncio.shield(self.tasks[c]) for c in child_ids))
        return tuple(ChildOutcome(c, self.processes[c].state, result)
                     for c, result in zip(child_ids, results))
