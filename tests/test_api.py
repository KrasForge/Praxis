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


def test_inspection_tree_is_read_only(tmp_path):
    from praxis.kernel.spec import ProcessSpec

    async def exercise():
        kernel = make_kernel(tmp_path)
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.create(ProcessSpec("child", "fake"), parent.process_id)
        kernel.start(child.process_id)
        await kernel.tasks[child.process_id]
        app = Application(ControlPlane(kernel))
        before = tuple(e.to_json() for e in kernel.events)
        status, tree = await request(app, "GET", f"/v1/processes/{parent.process_id}/tree")
        assert status == 200 and len(tree["processes"]) == 2
        assert tree["processes"][0]["children"] == [child.process_id]
        detail = tree["processes"][1]
        assert detail["parent_id"] == parent.process_id
        assert detail["result"]["state"] == "completed" and detail["verification"]["approved"]
        assert "wall_milliseconds" in detail["usage"]
        assert (await request(app, "GET", "/v1/processes/missing"))[0] == 404
        assert tuple(e.to_json() for e in kernel.events) == before
    asyncio.run(exercise())


def test_sse_reconnect_and_tree_cursors(tmp_path):
    from praxis.kernel.spec import ProcessSpec

    async def exercise():
        kernel = make_kernel(tmp_path)
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.create(ProcessSpec("child", "fake"), parent.process_id)
        kernel.start(child.process_id)
        kernel.start(parent.process_id)
        await asyncio.gather(*kernel.tasks.values())
        service = ControlPlane(kernel)
        app = Application(service)
        complete = service.events(parent.process_id, tree=True)
        assert {entry.event.process_id for entry in complete} == {parent.process_id, child.process_id}
        async def collect(after, disconnect_after=None):
            incoming = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": b""})
            events = []
            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    raw = message["body"].decode().split("data: ", 1)[1].strip()
                    events.append(json.loads(raw))
                    if len(events) == disconnect_after:
                        await incoming.put({"type": "http.disconnect"})
            await app({"type": "http", "method": "GET", "path": f"/v1/processes/{parent.process_id}/events",
                       "query_string": b"tree=true", "headers": [(b"last-event-id", str(after).encode())]}, incoming.get, send)
            return events
        first = await collect(0, 3)
        second = await collect(first[-1]["cursor"])
        assert [e["cursor"] for e in first + second] == [entry.cursor for entry in complete]
        assert len({e["event"]["event_id"] for e in first + second}) == len(complete)
    asyncio.run(exercise())


def test_controls_reject_stale_attempts_and_retry(tmp_path):
    from praxis.executors.outcomes import Outcome, OutcomeStatus

    async def exercise():
        kernel = make_kernel(tmp_path)
        kernel.executors["fake"] = FakeExecutor(Outcome(OutcomeStatus.FAILED, "transient", retryable=True))
        app = Application(ControlPlane(kernel))
        _, submitted = await request(app, "POST", "/v1/processes", {"objective": "task", "executor": "fake"})
        pid = submitted["process_id"]
        await kernel.tasks[pid]
        old = kernel.processes[pid].attempt_id
        path = f"/v1/processes/{pid}/control"
        assert (await request(app, "POST", path, {"operation": "cancel", "attempt_id": "old"}))[0] == 409
        kernel.executors["fake"] = FakeExecutor()
        status, retry = await request(app, "POST", path, {"operation": "retry", "attempt_id": old})
        assert status == 200 and retry["attempt_id"] != old
        await kernel.tasks[pid]
        assert (await request(app, "POST", path, {"operation": "suspend", "attempt_id": old}))[0] == 409
        status, signal = await request(app, "POST", path, {"operation": "signal", "signal": "interrupt", "attempt_id": retry["attempt_id"]})
        assert status == 200 and not signal["control"]["applied"]
        assert len([e for e in kernel.events if e.type == "api.control"]) == 4
    asyncio.run(exercise())


def test_pending_approvals_and_interventions(tmp_path):
    from praxis.kernel.capabilities import Resource
    from praxis.kernel.effect_service import EffectPolicy, EffectService
    from praxis.kernel.effects import message_send
    from praxis.kernel.spec import ProcessSpec
    from praxis.storage.sqlite import SQLiteStore

    async def exercise():
        authority = Authority(execution_defaults=frozenset({"fake"}))
        kernel = Kernel(SQLiteStore(tmp_path / "db"), LocalWorkspaces(tmp_path / "ws"), {"fake": FakeExecutor()}, authority=authority)
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.create(ProcessSpec("child", "fake"), parent.process_id)
        authority.issue(child.process_id, Resource.EFFECT, frozenset({"stage"}), "channel")
        authority.issue(child.process_id, Resource.EFFECT, frozenset({"apply"}), "message:channel")
        authority.issue(parent.process_id, Resource.EFFECT, frozenset({"approve"}), "channel")
        effects = EffectService(kernel.records, authority)
        staged = effects.stage(message_send(child.process_id, child.attempt_id, "channel", "fixture"))
        effects.apply_policy(staged.effect_id, staged.version, EffectPolicy.HUMAN, actor=parent.process_id, reason="review")
        app = Application(ControlPlane(kernel, effects))
        assert len((await request(app, "GET", f"/v1/processes/{parent.process_id}/approvals"))[1]["approvals"]) == 1
        path = f"/v1/processes/{child.process_id}/approvals"
        data = {"effect_id": staged.effect_id, "version": staged.version, "attempt_id": child.attempt_id,
                "actor": parent.process_id, "reason": "reviewed", "approved": False}
        status, result = await request(app, "POST", path, data)
        assert status == 200 and result["approvals"][0]["decision"] == "denied"
        assert (await request(app, "POST", path, data))[0] == 409
        path = f"/v1/processes/{child.process_id}/interventions"
        data = {"attempt_id": child.attempt_id, "actor": parent.process_id, "reason": "clarification",
                "kind": "instruction", "content": "Preserve source citations"}
        assert (await request(app, "POST", path, data))[1]["recorded"]
        assert (await request(app, "POST", path, {**data, "attempt_id": "old"}))[0] == 409
        assert child.spec.objective == "child"
    asyncio.run(exercise())
