"""Speculative children with held publication and independent budget reservations."""

import asyncio
from dataclasses import replace
from uuid import uuid4

from praxis.kernel.budgets import RESOURCES
from praxis.kernel.candidates import Candidate, CandidateGroup, CandidateState
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.transaction import CanonicalDirectory


class Speculation:
    def __init__(self, kernel: Kernel):
        self.kernel = kernel
        self.groups: dict[str, CandidateGroup] = {}

    def fork(self, parent_id: str, objective: str, specs: tuple[ProcessSpec, ...], *,
             canonical: CanonicalDirectory | None = None) -> CandidateGroup:
        if not specs or not objective.strip():
            raise ValueError("candidate specs and common objective required")
        kernel = self.kernel
        group_id = str(uuid4())
        validated = [ProcessSpec.from_json(replace(spec, objective=objective).to_json()) for spec in specs]
        for resource in RESOURCES:
            available = kernel.budgets.remaining(parent_id, resource)
            requests = [spec.budget.get(resource) for spec in validated]
            if available is not None and (any(value is None for value in requests)
                                          or sum(value or 0 for value in requests) > available):
                raise ValueError("candidate_budget_exceeds_parent")
        children = []
        try:
            for spec in validated:
                child = kernel.create(spec, parent_id, canonical=canonical)
                children.append(child)
                kernel.deferred_commits.add(child.process_id)
                kernel.events.append(Event(child.process_id, "candidate.isolated", {"group_id": group_id},
                                           parent_id=parent_id))
        except Exception:
            for child in children:
                kernel._move(child, State.CANCELLED)
                kernel.budgets.release(child.process_id)
            raise
        group = CandidateGroup(parent_id, objective, tuple(
            Candidate(str(uuid4()), child.process_id) for child in children), group_id=group_id)
        self._record(group)
        for child in children:
            kernel.start(child.process_id)
        group = group.move(CandidateState.RUNNING)
        self._record(group)
        return group

    async def collect(self, group_id: str) -> CandidateGroup:
        group = self.groups[group_id]
        if group.state != CandidateState.RUNNING:
            raise ValueError("group_not_running")
        await asyncio.gather(*(asyncio.shield(self.kernel.tasks[c.process_id]) for c in group.candidates))
        group = group.move(CandidateState.EVALUATING)
        self._record(group)
        return group

    def _record(self, group: CandidateGroup) -> None:
        self.kernel.events.append(Event(group.parent_id, "candidate.group", {"group": group.to_json()},
                                        parent_id=self.kernel.processes[group.parent_id].parent_id))
        self.groups[group.group_id] = group
