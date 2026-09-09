"""Capability matching, bounded dispatch, and explicit executor fallback."""

import asyncio
from dataclasses import dataclass

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.queue import QueueDecision, SchedulerQueue
from praxis.kernel.retry import RetryPolicy
from praxis.kernel.runtime import Kernel


@dataclass(frozen=True)
class PlacementPolicy:
    candidates: tuple[str, ...]
    required_features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.candidates or len(set(self.candidates)) != len(self.candidates):
            raise ValueError("explicit unique executor candidates required")


class Scheduler:
    def __init__(self, kernel: Kernel, *, concurrency: int = 4,
                 executor_limits: dict[str, int] | None = None):
        limits = executor_limits or {}
        if type(concurrency) is not int or concurrency < 1 or any(type(v) is not int or v < 1 for v in limits.values()):
            raise ValueError("positive concurrency limits required")
        self.kernel = kernel
        self.queue = SchedulerQueue()
        self.policies: dict[str, PlacementPolicy] = {}
        self.global_slots = asyncio.Semaphore(concurrency)
        self.slots = {name: asyncio.Semaphore(limits.get(name, concurrency)) for name in kernel.executors}
        self.active: dict[str, int] = {name: 0 for name in kernel.executors}

    def enqueue(self, process_id: str, policy: PlacementPolicy | None = None) -> None:
        process = self.kernel.processes[process_id]
        self.queue.enqueue(process)
        self.policies[process_id] = policy or PlacementPolicy((process.spec.executor,))
        self.kernel.events.append(Event(process_id, "scheduler.queued", parent_id=process.parent_id))

    async def drain(self) -> tuple[Outcome, ...]:
        decisions = []
        while (decision := self.queue.pop()) is not None:
            decisions.append(decision)
        return tuple(await asyncio.gather(*(self._dispatch(decision) for decision in decisions)))

    def _fail(self, process_id: str, reason: str) -> Outcome:
        process = self.kernel.processes[process_id]
        result = Outcome(OutcomeStatus.UNAVAILABLE, reason)
        self.kernel.results[process_id] = result
        if process.state == State.PENDING:
            self.kernel._move(process, State.FAILED)
            self.kernel.budgets.release(process_id)
        self.kernel.events.append(Event(process_id, "scheduler.blocked", {"reason": reason}, parent_id=process.parent_id))
        return result

    async def _dispatch(self, decision: QueueDecision) -> Outcome:
        process_id = decision.entry.process_id
        process = self.kernel.processes[process_id]
        if process.attempt_id != decision.entry.attempt_id or process.state != State.PENDING:
            return Outcome(OutcomeStatus.UNAVAILABLE, "stale_queue_entry")
        if decision.expired:
            return self._fail(process_id, "deadline_expired")
        policy = self.policies[process_id]
        compatible = [name for name in policy.candidates if name in self.kernel.executors
                      and self.kernel.executors[name].descriptor.matches(policy.required_features)]
        if not compatible:
            return self._fail(process_id, "no_compatible_executor")
        async with self.global_slots:
            result = Outcome(OutcomeStatus.UNAVAILABLE, "executor_unavailable")
            for name in compatible:
                async with self.slots[name]:
                    if process.spec.deadline is not None:
                        from datetime import datetime, timezone
                        if datetime.fromisoformat(process.spec.deadline) <= datetime.now(timezone.utc):
                            return self._fail(process_id, "deadline_expired")
                    self.active[name] += 1
                    self.kernel.assignments[process_id] = name
                    self.kernel.events.append(Event(process_id, "scheduler.placed", {
                        "executor": name, "wait_seconds": decision.wait_seconds,
                        "attempt_id": process.attempt_id,
                    }, parent_id=process.parent_id))
                    try:
                        if process.state == State.PENDING:
                            self.kernel.start(process_id)
                        else:
                            await self.kernel.retry(process_id, RetryPolicy(
                                max_attempts=len(compatible), retryable_reasons=frozenset({result.reason}),
                            ))
                        result = await asyncio.shield(self.kernel.tasks[process_id])
                    finally:
                        self.active[name] -= 1
                if result.status != OutcomeStatus.UNAVAILABLE and not result.retryable:
                    break
            return result
