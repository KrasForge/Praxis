"""Orphan recovery requires positive fencing, never a timeout alone."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.remote import RemoteExecutor
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.process import now
from praxis.kernel.retry import RetryPolicy
from praxis.remote.placement import DistributedScheduler, Placement, WorkerPlacementPolicy
from praxis.remote.workers import WorkerError


@dataclass(frozen=True)
class RecoveryRecord:
    process_id: str
    attempt_id: str
    worker_id: str
    generation: int
    state: str = "orphaned"
    reason: str = "worker_unavailable"
    replacement_attempt: str | None = None


class OrphanRecovery:
    def __init__(self, scheduler: DistributedScheduler):
        self.locks: dict[str, asyncio.Lock] = {}
        self.scheduler = scheduler
        self.kernel = scheduler.kernel
        self.store = scheduler.placement.store
        with self.store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS orphan_recovery (process_id TEXT, attempt_id TEXT, body TEXT, PRIMARY KEY(process_id,attempt_id))")

    def detect(self) -> tuple[RecoveryRecord, ...]:
        alive = self.scheduler.placement.heartbeats.available(include_saturated=True)
        records = []
        with self.store._transaction() as connection:
            assignments = list(connection.execute("SELECT body,state FROM worker_assignments WHERE state IN ('reserved','orphaned')"))
        for raw, state in assignments:
            placement = Placement(**json.loads(raw))
            process = self.kernel.processes[placement.process_id]
            if process.attempt_id != placement.attempt_id:
                continue
            generation = self.scheduler.placement.heartbeats.registry.load(placement.worker_id).generation
            if state != "orphaned" and placement.worker_id in alive and generation == placement.generation:
                continue
            record = self.load(placement.process_id, placement.attempt_id)
            if record is None:
                record = RecoveryRecord(placement.process_id, placement.attempt_id, placement.worker_id, placement.generation)
                self._record(record)
            executor = self.kernel.executors.get(self.kernel.executor_name(process))
            if isinstance(executor, RemoteExecutor):
                executor.generation_current = lambda: False
            task = self.kernel.tasks.get(process.process_id)
            if process.state not in TERMINAL and (task is None or task.done()):
                self._fail_orphan(process.process_id)
            self.scheduler.placement.finish(placement, uncertain=True)
            records.append(record)
        return tuple(records)

    def _fail_orphan(self, process_id: str) -> None:
        process = self.kernel.processes[process_id]
        outcome = Outcome(OutcomeStatus.PARTIAL, "worker_orphaned")
        self.kernel.results[process_id] = outcome
        self.kernel._move(process, State.FAILED)
        self.kernel.budgets.release(process_id)
        self.kernel.events.append(Event(process_id, "process.outcome", json.loads(outcome.to_json()), parent_id=process.parent_id))

    def load(self, process_id: str, attempt_id: str) -> RecoveryRecord | None:
        with self.store._transaction() as connection:
            row = connection.execute("SELECT body FROM orphan_recovery WHERE process_id=? AND attempt_id=?", (process_id, attempt_id)).fetchone()
            return None if row is None else RecoveryRecord(**json.loads(row[0]))

    def _record(self, record: RecoveryRecord) -> None:
        process = self.kernel.processes[record.process_id]
        event = Event(record.process_id, "process.recovery", asdict(record), parent_id=process.parent_id)
        with self.store._transaction() as connection:
            connection.execute("INSERT INTO orphan_recovery VALUES(?,?,?) ON CONFLICT(process_id,attempt_id) DO UPDATE SET body=excluded.body",
                               (record.process_id, record.attempt_id, json.dumps(asdict(record))))
            connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)", (event.event_id, event.process_id, event.to_json()))

    async def relocate(self, process_id: str, policy: WorkerPlacementPolicy,
                       confirm_stopped: Callable[[RecoveryRecord], Awaitable[bool]], *, max_attempts: int = 3) -> Outcome:
        async with self.locks.setdefault(process_id, asyncio.Lock()):
            return await self._relocate(process_id, policy, confirm_stopped, max_attempts=max_attempts)

    async def _relocate(self, process_id: str, policy: WorkerPlacementPolicy,
                        confirm_stopped: Callable[[RecoveryRecord], Awaitable[bool]], *, max_attempts: int) -> Outcome:
        process = self.kernel.processes[process_id]
        record = self.load(process_id, process.attempt_id)
        if record is None or record.state != "orphaned":
            raise WorkerError("process_not_orphaned")
        family = {process_id}
        while True:
            expanded = family | {p.process_id for p in self.kernel.processes.values() if p.parent_id in family}
            if expanded == family:
                break
            family = expanded
        history = tuple(entry.event for entry in self.store.read_events())
        if any(e.process_id in family and (e.type == "workspace.committed" or
            e.type in {"effect.applying", "effect.applied"} and e.payload.get("replay_safe") is not True) for e in history):
            raise WorkerError("unsafe_effect_relocation")
        if await asyncio.wait_for(confirm_stopped(record), 30) is not True:
            raise WorkerError("worker_fencing_required")
        task = self.kernel.tasks.get(process_id)
        if task is not None and not task.done():
            await asyncio.wait_for(asyncio.shield(task), 30)
        if process.state not in TERMINAL:
            self._fail_orphan(process_id)
        if process.state != State.FAILED:
            raise WorkerError("orphan_no_longer_recoverable")
        # Charge the wall interval through confirmed fencing, including controller downtime.
        started = next((e.timestamp for e in history if e.process_id == process_id and
                        e.type == "worker.dispatch_pending" and e.payload.get("attempt_id") == process.attempt_id), None)
        if started is not None:
            elapsed = max(0, int((datetime.fromisoformat(now()) - datetime.fromisoformat(started)).total_seconds() * 1000))
            measured = self.kernel.usage.total(process_id, attempt_id=process.attempt_id).get("wall_milliseconds", 0)
            self.kernel.usage.record(process_id, process.attempt_id, process.attempt_id + ":fenced-wall",
                                     {"wall_milliseconds": max(0, elapsed - measured)})
        self.kernel.events.append(Event(process_id, "worker.execution_fenced", {
            "attempt_id": process.attempt_id, "worker_id": record.worker_id, "generation": record.generation,
        }, parent_id=process.parent_id))
        old_attempt = process.attempt_id
        with self.store._transaction() as connection:
            connection.execute("UPDATE worker_assignments SET state='released' WHERE process_id=? AND attempt_id=?", (process_id, old_attempt))
        outcome = self.kernel.results[process_id]
        await self.kernel.retry(process_id, RetryPolicy(max_attempts=max_attempts,
                                retryable_reasons=frozenset({outcome.reason})), start=False)
        self._record(replace(record, state="relocated", reason="worker_fenced", replacement_attempt=process.attempt_id))
        return await self.scheduler.run(process_id, policy)
