import json

import pytest

from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process, ProcessRecords
from praxis.kernel.spec import ProcessSpec


def test_nested_metadata_and_restart(tmp_path):
    store = ProcessRecords(tmp_path)
    parent = Process(ProcessSpec("parent", "fake"))
    child = Process(ProcessSpec("child", "fake"), parent_id=parent.process_id)
    grandchild = Process(ProcessSpec("grandchild", "fake"), parent_id=child.process_id)
    for process in (parent, child, grandchild):
        identity = process.process_id
        for state in (State.RUNNING, State.SUSPENDED, State.RUNNING, State.COMPLETED):
            process.move(state)
        store.save(process)
        restored = ProcessRecords(tmp_path).load(identity)
        assert restored == process
        assert len(restored.history) == 5
        assert restored.created_at == process.created_at
    assert store.load(grandchild.process_id).parent_id == child.process_id


def test_corrupt_history():
    process = Process(ProcessSpec("work", "fake"))
    data = json.loads(process.to_json())
    data["history"][0]["state"] = "completed"
    with pytest.raises(ValueError, match="corrupt"):
        Process.from_json(json.dumps(data))


def test_invalid_identity(tmp_path):
    with pytest.raises(ValueError):
        ProcessRecords(tmp_path).load("../escape")
