"""Explicit supervisor/fork orchestration over kernel process primitives."""

from dataclasses import dataclass

from praxis.executors.outcomes import Outcome
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec


@dataclass(frozen=True)
class DelegationRequest:
    capability_id: str
    actions: frozenset[str]
    scope: str
    max_bytes: int | None = None


@dataclass(frozen=True)
class ChildStatus:
    process_id: str
    state: State
    outcome: Outcome | None


class Supervisor:
    def __init__(self, kernel: Kernel, process_id: str):
        if process_id not in kernel.processes:
            raise ValueError("supervisor process not found")
        self.kernel = kernel
        self.process_id = process_id

    def fork(self, specs: tuple[ProcessSpec, ...], *,
             delegations: tuple[tuple[DelegationRequest, ...], ...] | None = None) -> tuple[str, ...]:
        if not specs:
            raise ValueError("fork requires candidates")
        grants = delegations if delegations is not None else tuple(() for _ in specs)
        if len(grants) != len(specs):
            raise ValueError("one explicit delegation set per child required")
        validated = tuple(ProcessSpec.from_json(spec.to_json()) for spec in specs)
        children = []
        try:
            for spec, requests in zip(validated, grants):
                child = self.kernel.create(spec, self.process_id)
                children.append(child.process_id)
                for request in requests:
                    self.kernel.authority.delegate(
                        self.process_id, child.process_id, request.capability_id,
                        actions=request.actions, scope=request.scope, max_bytes=request.max_bytes,
                    )
        except Exception:
            for identity in children:
                child = self.kernel.processes[identity]
                self.kernel._move(child, State.CANCELLED)
                self.kernel.budgets.release(identity)
            self.kernel.events.append(Event(self.process_id, "supervisor.fork_failed", {"children": children},
                                            parent_id=self.kernel.processes[self.process_id].parent_id))
            raise
        for identity in children:
            self.kernel.start(identity)
        self.kernel.events.append(Event(self.process_id, "supervisor.forked", {"children": children},
                                        parent_id=self.kernel.processes[self.process_id].parent_id))
        return tuple(children)

    def monitor(self) -> tuple[ChildStatus, ...]:
        return tuple(ChildStatus(process.process_id, process.state, self.kernel.results.get(process.process_id))
                     for process in self.kernel.processes.values() if process.parent_id == self.process_id)
