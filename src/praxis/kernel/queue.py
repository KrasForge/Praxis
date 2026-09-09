"""Deterministic scheduler queue ordered by priority, deadline, then FIFO."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process


@dataclass(frozen=True)
class QueueEntry:
    process_id: str
    attempt_id: str
    priority: int
    deadline: datetime | None
    queued_at: datetime
    sequence: int


@dataclass(frozen=True)
class QueueDecision:
    entry: QueueEntry
    expired: bool
    wait_seconds: float


class SchedulerQueue:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.entries: dict[str, QueueEntry] = {}
        self.sequence = 0

    def enqueue(self, process: Process) -> QueueEntry:
        if process.state != State.PENDING or process.process_id in self.entries:
            raise ValueError("process is not newly runnable")
        now = self.clock()
        if now.utcoffset() is None:
            raise ValueError("queue clock requires timezone")
        deadline = None if process.spec.deadline is None else datetime.fromisoformat(process.spec.deadline)
        entry = QueueEntry(process.process_id, process.attempt_id, process.spec.priority,
                           deadline, now, self.sequence)
        self.sequence += 1
        self.entries[process.process_id] = entry
        return entry

    def pop(self) -> QueueDecision | None:
        if not self.entries:
            return None
        now = self.clock()
        expired = [entry for entry in self.entries.values() if entry.deadline is not None and entry.deadline <= now]
        if expired:
            selected = min(expired, key=lambda entry: (entry.deadline, entry.sequence))
        else:
            maximum = datetime.max.replace(tzinfo=timezone.utc)
            selected = min(self.entries.values(), key=lambda entry: (-entry.priority, entry.deadline or maximum, entry.sequence))
        del self.entries[selected.process_id]
        return QueueDecision(selected, bool(expired), max(0, (now - selected.queued_at).total_seconds()))

    def remove(self, process_id: str) -> QueueEntry:
        return self.entries.pop(process_id)

    def starved(self, *, after_seconds: float = 60) -> tuple[str, ...]:
        if after_seconds < 0:
            raise ValueError("invalid starvation threshold")
        now = self.clock()
        return tuple(entry.process_id for entry in sorted(self.entries.values(), key=lambda e: e.sequence)
                     if (now - entry.queued_at).total_seconds() >= after_seconds)
