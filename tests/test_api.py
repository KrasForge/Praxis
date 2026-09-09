import asyncio
import json

from praxis.api.asgi import Application
from praxis.api.service import ControlPlane
from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.runtime import Kernel
from praxis.storage.memory import MemoryStore
from praxis.workspaces.local import LocalWorkspaces


async def request(app, method, path, data=None, headers=()):
    messages = []
    async def receive():
        return {"type": "http.request", "body": json.dumps(data or {}).encode()}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "method": method, "path": path, "headers": headers}, receive, send)
    return messages[0]["status"], json.loads(messages[1]["body"])


def make_kernel(tmp_path):
    return Kernel(MemoryStore(), LocalWorkspaces(tmp_path), {"fake": FakeExecutor()},
                  authority=Authority(execution_defaults=frozenset({"fake"})))


def test_submission_api(tmp_path):
    async def exercise():
        kernel = make_kernel(tmp_path)
        app = Application(ControlPlane(kernel))
        spec = {"objective": "task", "executor": "fake"}
        headers = ((b"idempotency-key", b"request-1"),)
        status, first = await request(app, "POST", "/v1/processes", spec, headers)
        assert status == 202 and first["state"] == "pending"
        status, duplicate = await request(app, "POST", "/v1/processes", spec, headers)
        assert status == 200 and duplicate["process_id"] == first["process_id"]
        assert (await request(app, "POST", "/v1/processes", {**spec, "objective": "other"}, headers))[0] == 409
        assert (await request(app, "POST", "/v1/processes", {"executor": "fake"}))[0] == 422
        assert len(kernel.processes) == 1
        await kernel.tasks[first["process_id"]]
        recovered = Application(ControlPlane(kernel))
        assert (await request(recovered, "POST", "/v1/processes", spec, headers))[1]["duplicate"]
    asyncio.run(exercise())
