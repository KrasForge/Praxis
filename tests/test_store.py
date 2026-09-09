from dataclasses import replace

import pytest

from praxis.executors.protocol import Checkpoint
from praxis.kernel.events import Event
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.memory import MemoryStore
from praxis.storage.protocol import ProcessStore, StoredCheckpoint, StoreConflict, StoreError


def exercise_store(store: ProcessStore):
    process = Process(ProcessSpec("work", "fake"))
    event = Event(process.process_id, "process.created")
    store.save(process, (event,))
    assert store.load(process.process_id) == process
    store.save(process, (event,))
    assert len(store.read_events()) == 1
    assert not store.read_events(after=1)
    with pytest.raises(StoreConflict):
        store.save(process, (replace(event, type="forged"),))
    assert store.read_events()[0].event == event
    checkpoint = StoredCheckpoint(Checkpoint("fake", process.process_id, process.attempt_id, b"data"), "snapshot")
    store.save_checkpoint(checkpoint)
    assert store.load_checkpoint(process.process_id) == checkpoint
    assert store.list_processes() == (process.process_id,)
    store.close()
    with pytest.raises(StoreError):
        store.load(process.process_id)


def test_memory_conformance():
    store: ProcessStore = MemoryStore()
    assert isinstance(store, ProcessStore)
    exercise_store(store)
