import asyncio
import json

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.contracts import Check, Contract
from praxis.kernel.provenance import ProvenanceError, ProvenanceQueries
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore
from praxis.validators.protocol import FakeValidator
from praxis.workspaces.local import LocalWorkspaces


def test_reconstruct_persisted_process_tree_and_causal_events(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.db"
        store = SQLiteStore(path)
        kernel = Kernel(store, LocalWorkspaces(tmp_path / "ws"), {"fake": FakeExecutor()},
                        authority=Authority(execution_defaults=frozenset({"fake"})), validators={"fake": FakeValidator()})
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.create(ProcessSpec("child", "fake"), parent.process_id)
        grandchild = kernel.spawn(child.process_id, ProcessSpec("grandchild", "fake",
                                 contract=json.loads(Contract(validators=(Check("c", "fake"),)).to_json())))
        await kernel.join(child.process_id, [grandchild])
        store.close()
        reopened = SQLiteStore(path)
        queries = ProvenanceQueries(reopened)
        assert queries.ancestors(grandchild) == (parent.process_id, child.process_id)
        assert queries.descendants(parent.process_id) == (child.process_id, grandchild)
        events = queries.causal_sequence(parent.process_id)
        assert [e.cursor for e in events] == sorted(e.cursor for e in events)
        assert any(e.event.type == "contract.checked" for e in events)
        invocation = next(e for e in events if e.event.type == "executor.invoked")
        reopened.connection.execute("DELETE FROM events WHERE event_id=?", (invocation.event.event_id,))
        reopened.connection.commit()
        with pytest.raises(ProvenanceError, match="causal"):
            queries.causal_sequence(parent.process_id)
        reopened.close()
    asyncio.run(exercise())
