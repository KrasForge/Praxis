"""TM-6/TM-9 forged wire envelopes and replay regressions."""
import asyncio
import json
from dataclasses import replace

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.protocol import Checkpoint, ExecutionRequest
from praxis.executors.remote import RemoteExecutor
from praxis.kernel.spec import ProcessSpec
from praxis.remote.node import WorkerNode
from praxis.remote.workers import Worker, WorkerError
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("attack", ["process_id", "attempt_id", "generation", "lineage", "trace", "type", "cursor", "malformed"])
def test_forged_remote_responses(tmp_path, attack):
    async def exercise():
        worker = Worker("w", "owner", "boot")
        ws = LocalWorkspaces(tmp_path / "worker")
        node = WorkerNode(worker, SQLiteStore(tmp_path / "db"), ws, {"fake": FakeExecutor()}, lambda token: token == "valid")
        class Transport:
            async def request(self, method, path, body=None):
                response = await node.rpc(body, "valid")
                if body["operation"] == "poll":
                    await asyncio.gather(*node.tasks.values())
                    response = await node.rpc(body, "valid")
                    event = response["events"][0]
                    if attack == "malformed":
                        return []
                    if attack in {"process_id", "attempt_id", "generation"}:
                        response[attack] = "forged"
                    elif attack == "type":
                        event["event"]["type"] = "capability.issued"
                    elif attack == "cursor":
                        event["cursor"] = 0
                    else:
                        event["event"]["payload"][attack] = "forged"
                return response
        controller = LocalWorkspaces(tmp_path / "controller")
        handle = controller.create("p")
        adapter = RemoteExecutor(worker, "fake", controller, Transport())
        request = ExecutionRequest("p", "a", ProcessSpec("task", "fake"), handle.workspace_id, controller.path_for(handle, "p"))
        assert (await adapter.start(request)).applied
        assert (await adapter.collect_result("a")).reason == "remote_protocol_error"
        assert not adapter.results
        dispatch = adapter.dispatches["a"]
        with pytest.raises(WorkerError, match="unauthorized"):
            await node.rpc({"operation": "dispatch", "dispatch": json.loads(dispatch.to_json())}, "invalid")
        # A changed envelope cannot reuse the same process-attempt identity.
        changed = replace(dispatch, spec_json=ProcessSpec("different", "fake").to_json())
        with pytest.raises(WorkerError, match="identity_conflict"):
            await node.rpc({"operation": "dispatch", "dispatch": json.loads(changed.to_json())}, "valid")
        with pytest.raises(ValueError):
            replace(dispatch, protocol_version=2)
        with pytest.raises(ValueError):
            replace(dispatch, attempt_id="forged")
    asyncio.run(exercise())


@pytest.mark.parametrize("change", [{"protocol_version": 2}, {"protocol_version": True}, {"payload": "not bytes"}, {"process_id": None}])
def test_malformed_checkpoint_fixture(change):
    with pytest.raises(ValueError):
        Checkpoint(**{"executor": "fake", "process_id": "p", "attempt_id": "a", "payload": b"data", **change})
