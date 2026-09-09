import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import OutcomeStatus
from praxis.kernel.authority import Authority
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.remote.heartbeat import Heartbeats, WorkerCapabilities
from praxis.remote.node import WorkerNode
from praxis.remote.placement import DistributedScheduler, WorkerPlacement, WorkerPlacementPolicy
from praxis.remote.workers import WorkerError, WorkerRegistry
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


def test_placement_capacity_locality_and_dispatch(tmp_path):
    async def exercise():
        store = SQLiteStore(tmp_path / "db")
        registry = WorkerRegistry(store, lambda credential, identity: "owner")
        clock = [100.0]
        heartbeats = Heartbeats(registry, ttl_seconds=10, clock=lambda: clock[0])
        for identity, locality in [("a", "east"), ("b", "west")]:
            registry.register(identity, "boot", 1, "credential")
            heartbeats.heartbeat(identity, 1, 1, WorkerCapabilities((ExecutorFeatures("fake"),), frozenset({"local"}), 1, locality), "credential")
        kernel = Kernel(store, LocalWorkspaces(tmp_path / "controller"), {},
                        authority=Authority(execution_defaults=frozenset({"fake"})))
        placement = WorkerPlacement(heartbeats)
        first = kernel.create(ProcessSpec("first", "fake"))
        policy = WorkerPlacementPolicy("fake", locality="west")
        with pytest.raises(WorkerError, match="no_compatible"):
            placement.reserve(first, WorkerPlacementPolicy("fake", required_features=frozenset({"checkpoint"})))
        reserved = placement.reserve(first, policy)
        assert reserved.worker_id == "b"
        second = kernel.create(ProcessSpec("second", "fake"))
        another = placement.reserve(second, policy)
        assert another.worker_id == "a"
        third = kernel.create(ProcessSpec("third", "fake"))
        with pytest.raises(WorkerError, match="no_compatible"):
            placement.reserve(third, policy)
        placement.finish(reserved)
        placement.finish(another)
        class Transport:
            def __init__(self, worker):
                provider = LocalWorkspaces(tmp_path / worker.worker_id)
                self.node = WorkerNode(worker, SQLiteStore(tmp_path / (worker.worker_id + ".db")), provider,
                                       {"fake": FakeExecutor()}, lambda credential: True)
            async def request(self, method, path, body=None):
                return await self.node.rpc(body, "credential")
        scheduler = DistributedScheduler(kernel, placement, Transport)
        assert (await scheduler.run(third.process_id, policy)).status == OutcomeStatus.COMPLETED
        assert kernel.result(third.process_id).usage["wall_milliseconds"] >= 0
        clock[0] += 10
        fourth = kernel.create(ProcessSpec("fourth", "fake"))
        with pytest.raises(WorkerError, match="no_compatible"):
            placement.reserve(fourth, policy)
        assert any(e.event.type == "worker.placed" for e in store.read_events())
    asyncio.run(exercise())
