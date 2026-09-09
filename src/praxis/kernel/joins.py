"""All/any/quorum joins with explicit child failure handling."""

import asyncio
from dataclasses import dataclass
from enum import Enum

from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.retry import RetryError, RetryPolicy
from praxis.kernel.runtime import ChildOutcome, Kernel


class JoinPolicy(str, Enum):
    ALL = "all"
    ANY = "any"
    QUORUM = "quorum"


class ChildFailurePolicy(str, Enum):
    CONTINUE = "continue"
    FAIL_FAST = "fail_fast"
    RETRY = "retry"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class JoinReport:
    status: str
    outcomes: tuple[ChildOutcome, ...]
    pending: tuple[str, ...]


async def join_children(
    kernel: Kernel, parent_id: str, children: tuple[str, ...],
    *, policy: JoinPolicy = JoinPolicy.ALL, quorum: int | None = None,
    failure_policy: ChildFailurePolicy = ChildFailurePolicy.CONTINUE,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> JoinReport:
    if not children or len(set(children)) != len(children):
        raise ValueError("unique children required")
    if not isinstance(policy, JoinPolicy) or not isinstance(failure_policy, ChildFailurePolicy):
        raise ValueError("typed join policies required")
    if policy == JoinPolicy.QUORUM:
        if type(quorum) is not int or not 1 <= quorum <= len(children):
            raise ValueError("invalid join quorum")
        threshold = quorum
    else:
        if quorum is not None:
            raise ValueError("quorum only applies to quorum joins")
        threshold = len(children) if policy == JoinPolicy.ALL else 1
    for child in children:
        if kernel.processes[child].parent_id != parent_id:
            raise ValueError("join requires direct children")
        if kernel.processes[child].state not in TERMINAL and child not in kernel.tasks:
            raise ValueError("child has not started")
    exhausted: set[str] = set()
    while True:
        outcomes = tuple(ChildOutcome(c, kernel.processes[c].state, kernel.results[c])
                         for c in children if kernel.processes[c].state in TERMINAL)
        failures = [outcome for outcome in outcomes if outcome.state != State.COMPLETED]
        if failure_policy == ChildFailurePolicy.RETRY:
            restarted = False
            for outcome in failures:
                if outcome.process_id in exhausted:
                    continue
                try:
                    await kernel.retry(outcome.process_id, retry_policy)
                    restarted = True
                except RetryError:
                    exhausted.add(outcome.process_id)
            if restarted:
                continue
        pending = tuple(c for c in children if kernel.processes[c].state not in TERMINAL)
        successes = sum(outcome.state == State.COMPLETED for outcome in outcomes)
        if failures and failure_policy == ChildFailurePolicy.ESCALATE:
            status = "escalated"
        elif failures and failure_policy == ChildFailurePolicy.FAIL_FAST:
            status = "failed"
        elif successes >= threshold:
            status = "completed"
        elif successes + len(pending) < threshold or not pending:
            status = "failed"
        else:
            await asyncio.wait([kernel.tasks[c] for c in pending], return_when=asyncio.FIRST_COMPLETED)
            continue
        report = JoinReport(status, outcomes, pending)
        parent = kernel.processes[parent_id]
        kernel.events.append(Event(parent_id, "supervisor.joined", {
            "policy": policy.value, "failure_policy": failure_policy.value,
            "threshold": threshold, "status": status,
            "outcomes": {outcome.process_id: outcome.state.value for outcome in outcomes},
            "pending": list(pending),
        }, parent_id=parent.parent_id))
        return report
