from datetime import datetime, timedelta, timezone

from praxis.kernel.process import Process
from praxis.kernel.queue import SchedulerQueue
from praxis.kernel.spec import ProcessSpec


def test_priority_deadline_fifo_and_expiration():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue = SchedulerQueue(lambda: now)
    low = Process(ProcessSpec("low", "fake", priority=0))
    high = Process(ProcessSpec("high", "fake", priority=10))
    urgent = Process(ProcessSpec("urgent", "fake", priority=10, deadline=(now + timedelta(seconds=5)).isoformat()))
    for process in (low, high, urgent):
        queue.enqueue(process)
    assert queue.pop().entry.process_id == urgent.process_id
    assert queue.pop().entry.process_id == high.process_id
    now += timedelta(seconds=61)
    assert queue.starved() == (low.process_id,)
    assert queue.pop().wait_seconds == 61
    expired = Process(ProcessSpec("expired", "fake", deadline=(now - timedelta(seconds=1)).isoformat()))
    queue.enqueue(expired)
    assert queue.pop().expired
    assert queue.pop() is None
