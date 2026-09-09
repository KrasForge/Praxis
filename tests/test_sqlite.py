from dataclasses import replace

import pytest

from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.protocol import StoreConflict, StoreError
from praxis.storage.sqlite import SQLiteStore
from test_store import exercise_store


def test_sqlite_conformance(tmp_path):
    exercise_store(SQLiteStore(tmp_path / "runtime.db"))


def test_restart_and_atomic_failure(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteStore(path)
    process = Process(ProcessSpec("work", "fake"))
    event = Event(process.process_id, "created")
    store.save(process, (event,))
    process.move(State.RUNNING)
    with pytest.raises(StoreConflict):
        store.save(process, (replace(event, type="conflict"),))
    assert store.load(process.process_id).state == State.PENDING
    running = Event(process.process_id, "running")
    store.save(process, (running,))
    store.close()
    reopened = SQLiteStore(path)
    assert reopened.load(process.process_id).state == State.RUNNING
    assert [e.event.type for e in reopened.read_events()] == ["created", "running"]
    reopened.connection.execute("UPDATE processes SET body='corrupt'")
    reopened.connection.commit()
    with pytest.raises(StoreError, match="corrupt"):
        reopened.load(process.process_id)
    reopened.close()
