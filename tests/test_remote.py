import asyncio
import json
import sys

from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.executors.remote import RemoteExecutor
from praxis.kernel.spec import ProcessSpec
from praxis.remote.asgi import WorkerApplication
from praxis.remote.node import WorkerNode
from praxis.remote.workers import Worker
from praxis.storage.sqlite import SQLiteStore
from praxis.transport.http import TransportError
from praxis.workspaces.local import LocalWorkspaces


class WorkerTransport:
    def __init__(self, app):
        self.app = app
        self.offline = False
    async def request(self, method, path, body=None):
        if self.offline:
            raise TransportError("transport_unavailable")
        sent = []
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode()}
        async def send(message):
            sent.append(message)
        await self.app({"type": "http", "method": method, "path": path,
                        "headers": [(b"authorization", b"Bearer kernel")]}, receive, send)
        if sent[0]["status"] != 200:
            raise TransportError("http_error", sent[0]["status"])
        return json.loads(sent[1]["body"])


def test_remote_dispatch_workspace_events_and_durable_result(tmp_path):
    async def exercise():
        worker = Worker("w", "operator", "boot")
        worker_ws = LocalWorkspaces(tmp_path / "worker")
        store = SQLiteStore(tmp_path / "worker.db")
        node = WorkerNode(worker, store, worker_ws, {"local": LocalProcessExecutor(worker_ws)}, lambda token: token == "Bearer kernel")
        transport = WorkerTransport(WorkerApplication(node))
        controller_ws = LocalWorkspaces(tmp_path / "controller")
        adapter = RemoteExecutor(worker, "local", controller_ws, transport)
        handle = controller_ws.create("p")
        path = controller_ws.path_for(handle, "p")
        (path / "seed").write_text("input")
        spec = ProcessSpec("task", "remote", inputs={"argv": [sys.executable, "-c",
            "from pathlib import Path; Path('output').write_text(Path('seed').read_text() + '-remote')"]})
        request = ExecutionRequest("p", "a", spec, handle.workspace_id, path)
        assert (await adapter.start(request)).applied
        outcome = await adapter.collect_result("a")
        assert outcome.status == OutcomeStatus.COMPLETED
        assert (path / "output").read_text() == "input-remote"
        assert [event.type for event in adapter.events] == ["worker.execution_started", "worker.execution_completed"]
        identity = adapter.dispatches["a"].execution_id
        store.close()
        reopened = SQLiteStore(tmp_path / "worker.db")
        recovered = WorkerNode(worker, reopened, worker_ws, {}, lambda token: True)
        reply = await recovered.rpc({"operation": "poll", "execution_id": identity}, "token")
        assert reply["result"]["outcome"]["status"] == "completed"
        transport.offline = True
        other = ExecutionRequest("p", "b", spec, handle.workspace_id, path)
        assert (await adapter.start(other)).reason == "remote_transport_unavailable"
        reopened.close()
    asyncio.run(exercise())
