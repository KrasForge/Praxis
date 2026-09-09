import asyncio

from praxis.api.service import ControlPlane
from praxis.kernel.scheduler import Scheduler
from praxis.kernel.spec import ProcessSpec
from praxis.observability.health import RuntimeHealth
from test_api import make_kernel, request, trusted_app


def test_readonly_health_and_pressure(tmp_path):
    async def exercise():
        kernel = make_kernel(tmp_path)
        scheduler = Scheduler(kernel, concurrency=1)
        health = RuntimeHealth(kernel, scheduler)
        assert health.snapshot()["status"] == "healthy"
        await scheduler.global_slots.acquire()
        assert health.snapshot()["status"] == "saturated"
        scheduler.global_slots.release()
        process = kernel.create(ProcessSpec("wait", "fake", budget={"wall_milliseconds": 0}))
        scheduler.enqueue(process.process_id)
        before = tuple(e.to_json() for e in kernel.events)
        service = ControlPlane(kernel, health=health)
        status, response = await request(trusted_app(service), "GET", "/v1/health")
        assert status == 200 and response["status"] == "degraded"
        assert response["queue"]["depth"] == 1 and response["budget_pressure"]
        assert service.inspect(process.process_id)["blocking_reason"] == "queued"
        assert tuple(e.to_json() for e in kernel.events) == before
        kernel.executors.clear()
        assert health.snapshot()["status"] == "unavailable"
    asyncio.run(exercise())


def test_worker_health_does_not_refresh_lease(tmp_path):
    from praxis.executors.features import ExecutorFeatures
    from praxis.remote.heartbeat import Heartbeats, WorkerCapabilities
    from praxis.remote.workers import WorkerRegistry
    from praxis.storage.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "db")
    registry = WorkerRegistry(store, lambda *_: "owner")
    registry.register("w", "boot", 1, "credential")
    clock = [0]
    heartbeats = Heartbeats(registry, ttl_seconds=10, clock=lambda: clock[0])
    health = RuntimeHealth(make_kernel(tmp_path / "work"), workers=heartbeats)
    assert health.snapshot()["workers"]["w"]["status"] == "unavailable"
    heartbeats.heartbeat("w", 1, 1, WorkerCapabilities((ExecutorFeatures("fake"),), frozenset({"local"}), 0), "credential")
    before = registry.events("w")
    assert health.snapshot()["workers"]["w"]["status"] == "saturated"
    clock[0] = 11
    assert health.snapshot()["workers"]["w"]["status"] == "unavailable"
    assert registry.events("w") == before
    store.close()
