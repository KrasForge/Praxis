import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.memory import MemoryStore
from praxis.storage.protocol import StoreConflict
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_stale_writers_cannot_rewind_state(tmp_path, backend):
    store = MemoryStore() if backend == "memory" else SQLiteStore(tmp_path / "runtime.db")
    process = Process(ProcessSpec("work", "fake"))
    store.save(process)
    stale = store.load(process.process_id)
    process.move(State.RUNNING)
    store.save(process)
    with pytest.raises(StoreConflict):
        store.save(stale)
    assert store.load(process.process_id).state == State.RUNNING
    store.close()


def test_kernel_events_survive_reopen_in_order(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.db"
        store = SQLiteStore(path)
        kernel = Kernel(store, LocalWorkspaces(tmp_path / "ws"), {"fake": FakeExecutor()},
                        authority=Authority(execution_defaults=frozenset({"fake"})))
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.spawn(parent.process_id, ProcessSpec("child", "fake"))
        await kernel.join(parent.process_id, [child])
        store.close()
        reopened = SQLiteStore(path)
        events = reopened.read_events(child)
        assert events[0].event.type == "process.created"
        assert all(e.event.parent_id == parent.process_id for e in events)
        assert [e.cursor for e in events] == sorted({e.cursor for e in events})
        assert any(e.event.type == "contract.evaluated" for e in events)
        assert reopened.load(child).state == State.COMPLETED
        reopened.close()
    asyncio.run(exercise())
