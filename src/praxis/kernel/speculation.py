"""Speculative children with held publication and independent budget reservations."""

import asyncio
import json
from dataclasses import asdict, replace
from uuid import uuid4

from praxis.evaluators.protocol import Evaluation, EvaluationInput, Evaluator, run_evaluator
from praxis.evaluators.selection import Selection, SelectionPolicy
from praxis.kernel.capabilities import Resource
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

    async def select(self, group_id: str, policy: SelectionPolicy, evaluators: tuple[Evaluator, ...],
                     rubric_json: str, *, human_choice: str | None = None,
                     actor: str | None = None) -> Selection:
        group = self.groups[group_id]
        if group.state == CandidateState.RUNNING:
            group = group.move(CandidateState.EVALUATING)
            self._record(group)
        if group.state != CandidateState.EVALUATING:
            raise ValueError("group_not_evaluating")
        known = {c.candidate_id for c in group.candidates}
        if not policy.eligible_candidates <= known:
            raise ValueError("unknown_eligible_candidate")
        ready = tuple((c.candidate_id, self.kernel.result(c.process_id).to_json())
                      for c in group.candidates if self.kernel.processes[c.process_id].state == State.COMPLETED)
        eligible = policy.eligible_candidates & {identity for identity, _ in ready}
        policy = replace(policy, eligible_candidates=frozenset(eligible))
        evaluations: tuple[Evaluation, ...] = ()
        request = None
        if ready:
            request = EvaluationInput(group_id, ready, rubric_json)
            evaluations = tuple(await asyncio.gather(*(run_evaluator(e, request) for e in evaluators)))
        selection = policy.select(evaluations, human_choice=human_choice, actor=actor)
        self.kernel.events.append(Event(group.parent_id, "candidate.evaluated", {
            "group_id": group_id, "input": None if request is None else json.loads(json.dumps(asdict(request))),
            "selection": json.loads(json.dumps(asdict(selection))), "policy": {
                "mode": policy.mode, "candidates": sorted(policy.eligible_candidates),
                "evaluators": sorted(policy.eligible_evaluators), "quorum": policy.quorum},
        }, parent_id=self.kernel.processes[group.parent_id].parent_id))
        if selection.status == "selected":
            self._record(group.move(CandidateState.SELECTED, selection.winners[0]))
        return selection

    def commit_selected(self, group_id: str, candidate_id: str) -> None:
        group = self.groups[group_id]
        if group.state != CandidateState.SELECTED or group.selected_id != candidate_id:
            raise ValueError("candidate_not_selected")
        candidate = next(c for c in group.candidates if c.candidate_id == candidate_id)
        process_id = candidate.process_id
        report = self.kernel.verification.get(process_id)
        if report is None or not report.approved:
            raise ValueError("candidate_not_verified")
        self.kernel.authority.require(process_id, Resource.WORKSPACE, "commit", process_id)
        transaction = self.kernel.staged_transactions.get(process_id)
        if process_id in self.kernel.canonical_targets and transaction is None:
            raise ValueError("staged_transaction_unavailable")
        if transaction is not None:
            self.kernel.authority.require(process_id, Resource.FILESYSTEM, "write", str(transaction.canonical.root))
            self.kernel.events.append(transaction.commit(report))
        self.kernel.events.append(Event(process_id, "candidate.released", {"group_id": group_id},
                                        parent_id=group.parent_id))
        self.kernel.deferred_commits.discard(process_id)

    async def cancel_losers(self, group_id: str) -> None:
        group = self.groups[group_id]
        if group.state != CandidateState.SELECTED:
            raise ValueError("selection_required")
        for candidate in group.candidates:
            if candidate.candidate_id != group.selected_id:
                await self.kernel.cancel(candidate.process_id)

    def recover_groups(self) -> None:
        for event in self.kernel.events:
            if event.type == "candidate.group":
                group = CandidateGroup.from_json(event.payload["group"])
                self.groups[group.group_id] = group
