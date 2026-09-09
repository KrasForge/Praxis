import pytest

from praxis.remote.workers import Worker, WorkerError, WorkerRegistry
from praxis.storage.sqlite import SQLiteStore


def test_authenticated_registration_replacement(tmp_path):
    store = SQLiteStore(tmp_path / "db")
    registry = WorkerRegistry(store, lambda token, identity: "operator" if token == "valid" else None)
    with pytest.raises(WorkerError, match="unauthorized"):
        registry.register("worker", "boot1", 1, "invalid")
    with pytest.raises(WorkerError, match="incompatible"):
        registry.register("worker", "boot1", 2, "valid")
    worker = registry.register("worker", "boot1", 1, "valid")
    assert Worker.from_json(worker.to_json()) == worker
    assert registry.register("worker", "boot1", 1, "valid") == worker
    assert len(registry.events("worker")) == 1
    with pytest.raises(WorkerError, match="conflict"):
        registry.register("worker", "boot2", 1, "valid")
    replacement = registry.register("worker", "boot2", 1, "valid", replace_generation=1)
    assert replacement.generation == 2
    assert registry.load("worker") == replacement
    assert "valid" not in str(registry.events("worker"))
    store.close()


def test_heartbeat_liveness_and_capability_versions(tmp_path):
    from dataclasses import replace

    from praxis.executors.features import ExecutorFeatures
    from praxis.remote.heartbeat import Heartbeats, WorkerCapabilities

    store = SQLiteStore(tmp_path / "db")
    registry = WorkerRegistry(store, lambda token, identity: "owner" if token == "valid" else None)
    registry.register("w", "boot", 1, "valid")
    clock = [100.0]
    heartbeats = Heartbeats(registry, ttl_seconds=10, clock=lambda: clock[0])
    capabilities = WorkerCapabilities((ExecutorFeatures("local"),), frozenset({"local"}), 2)
    heartbeats.heartbeat("w", 1, 1, capabilities, "valid")
    assert heartbeats.available() == {"w": capabilities}
    with pytest.raises(WorkerError, match="replayed"):
        heartbeats.heartbeat("w", 1, 1, capabilities, "valid")
    with pytest.raises(WorkerError, match="stale_capability"):
        heartbeats.heartbeat("w", 1, 2, replace(capabilities, capacity=3), "valid")
    updated = replace(capabilities, capacity=3, version=2)
    heartbeats.heartbeat("w", 1, 2, updated, "valid")
    assert heartbeats.available()["w"].capacity == 3
    clock[0] += 10
    assert not heartbeats.available()
    registry.register("w", "next-boot", 1, "valid", replace_generation=1)
    with pytest.raises(WorkerError, match="stale_worker"):
        heartbeats.heartbeat("w", 1, 3, updated, "valid")
    store.close()


def test_registration_and_heartbeat_rpc(tmp_path):
    import asyncio
    import json

    from praxis.executors.features import ExecutorFeatures
    from praxis.remote.asgi import WorkerApplication
    from praxis.remote.heartbeat import Heartbeats, RegistryRPC, WorkerCapabilities

    async def exercise():
        registry = WorkerRegistry(SQLiteStore(tmp_path / "db"), lambda token, worker: "owner" if token == "Bearer worker" else None)
        heartbeats = Heartbeats(registry)
        app = WorkerApplication(RegistryRPC(heartbeats))
        async def call(body):
            sent = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode()}
            async def send(message):
                sent.append(message)
            await app({"type": "http", "method": "POST", "path": "/v1/worker",
                       "headers": [(b"authorization", b"Bearer worker")]}, receive, send)
            assert sent[0]["status"] == 200
            return json.loads(sent[1]["body"])
        assert (await call({"operation": "register", "worker_id": "w", "incarnation": "boot", "protocol_version": 1}))["worker"]["generation"] == 1
        capabilities = WorkerCapabilities((ExecutorFeatures("fake"),), frozenset({"local"}), 1)
        assert (await call({"operation": "heartbeat", "worker_id": "w", "generation": 1, "sequence": 1,
                           "capabilities": json.loads(capabilities.to_json())}))["accepted"]
        assert "w" in heartbeats.available()
    asyncio.run(exercise())
