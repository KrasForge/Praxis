import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import OutcomeStatus
from praxis.kernel.authority import Authority
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.remote.heartbeat import Heartbeats, WorkerCapabilities
from praxis.remote.node import WorkerNode
from praxis.remote.placement import DistributedScheduler, WorkerPlacement, WorkerPlacementPolicy
from praxis.remote.recovery import OrphanRecovery
from praxis.remote.workers import WorkerError, WorkerRegistry
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("unsafe", [False, True])
def test_crash_partition_and_fenced_relocation(tmp_path, unsafe):
    async def exercise():
        store = SQLiteStore(tmp_path / "db")
        registry = WorkerRegistry(store, lambda token, worker: "owner")
        clock = [100.0]
        heartbeats = Heartbeats(registry, ttl_seconds=10, clock=lambda: clock[0])
        caps = WorkerCapabilities((ExecutorFeatures("fake"),), frozenset({"local"}), 1)
        for worker in ("a", "b"):
            registry.register(worker, "boot", 1, "credential")
            heartbeats.heartbeat(worker, 1, 1, caps, "credential")
        workspace = LocalWorkspaces(tmp_path / "controller")
        kernel = Kernel(store, workspace, {}, authority=Authority(execution_defaults=frozenset({"fake"})))
        process = kernel.create(ProcessSpec("task", "fake"))
        placement = WorkerPlacement(heartbeats)
        placement.reserve(process, WorkerPlacementPolicy("fake", allowed_workers=frozenset({"a"})))
        kernel._move(process, State.RUNNING)
        kernel.events.append(Event(process.process_id, "worker.dispatch_pending", {"attempt_id": process.attempt_id}))
        if unsafe:
            kernel.events.append(Event(process.process_id, "effect.applied", {"replay_safe": False}))
        old_attempt = process.attempt_id
        # Simulate controller restart and worker a loss; only b renews its lease.
        kernel = Kernel(store, workspace, {})
        kernel.recover_records()
        clock[0] += 11
        heartbeats.heartbeat("b", 1, 2, caps, "credential")
        class Transport:
            def __init__(self, worker):
                provider = LocalWorkspaces(tmp_path / "worker-b")
                self.node = WorkerNode(worker, SQLiteStore(tmp_path / "worker.db"), provider,
                                       {"fake": FakeExecutor()}, lambda token: True)
            async def request(self, method, path, body=None):
                return await self.node.rpc(body, "credential")
        scheduler = DistributedScheduler(kernel, placement, Transport)
        recovery = OrphanRecovery(scheduler)
        assert recovery.detect()[0].state == "orphaned"
        async def stopped(record):
            return True
        async def partitioned(record):
            return False
        policy = WorkerPlacementPolicy("fake", allowed_workers=frozenset({"b"}))
        if unsafe:
            with pytest.raises(WorkerError, match="unsafe_effect"):
                await recovery.relocate(process.process_id, policy, stopped)
            assert kernel.processes[process.process_id].attempt_id == old_attempt
        else:
            with pytest.raises(WorkerError, match="fencing_required"):
                await recovery.relocate(process.process_id, policy, partitioned)
            assert (await recovery.relocate(process.process_id, policy, stopped)).status == OutcomeStatus.COMPLETED
            assert kernel.processes[process.process_id].attempt_id != old_attempt
            assert recovery.load(process.process_id, old_attempt).state == "relocated"
            assert kernel.usage.total(process.process_id)["wall_milliseconds"] >= 0
    asyncio.run(exercise())
