"""Transactional worker reservations and deterministic distributed placement."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.remote import RemoteExecutor
from praxis.kernel.capabilities import Resource
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process
from praxis.kernel.runtime import Kernel
from praxis.remote.heartbeat import Heartbeats
from praxis.remote.workers import Worker, WorkerError
from praxis.transport.http import JSONTransport


@dataclass(frozen=True)
class WorkerPlacementPolicy:
    executor: str
    workspace: str = "local"
    required_features: frozenset[str] = frozenset()
    locality: str = ""
    preferred_workers: tuple[str, ...] = ()
    allowed_workers: frozenset[str] | None = None


@dataclass(frozen=True)
class Placement:
    process_id: str
    attempt_id: str
    worker_id: str
    generation: int
    executor: str


class WorkerPlacement:
    def __init__(self, heartbeats: Heartbeats):
        self.heartbeats = heartbeats
        self.store = heartbeats.registry.store
        with self.store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS worker_assignments (process_id TEXT, attempt_id TEXT, "
                               "worker_id TEXT, generation INTEGER, state TEXT, body TEXT, PRIMARY KEY(process_id,attempt_id))")

    def reserve(self, process: Process, policy: WorkerPlacementPolicy) -> Placement:
        if process.state != State.PENDING:
            raise WorkerError("placement_requires_pending_process")
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM worker_assignments WHERE process_id=? AND attempt_id=?",
                                  (process.process_id, process.attempt_id)).fetchone():
                raise WorkerError("attempt_already_placed")
            available = self.heartbeats.available()
            eligible = []
            rejected = {}
            for identity, capabilities in available.items():
                if policy.allowed_workers is not None and identity not in policy.allowed_workers:
                    rejected[identity] = "policy_excluded"
                    continue
                if policy.workspace not in capabilities.workspaces or not any(
                    e.name == policy.executor and e.matches(policy.required_features) for e in capabilities.executors
                ):
                    rejected[identity] = "incompatible_capabilities"
                    continue
                worker = self.heartbeats.registry.load(identity)
                active = connection.execute("SELECT COUNT(*) FROM worker_assignments WHERE worker_id=? AND generation=? AND state IN ('reserved','orphaned')",
                                            (identity, worker.generation)).fetchone()[0]
                if active >= capabilities.capacity:
                    rejected[identity] = "capacity_exhausted"
                    continue
                preference = policy.preferred_workers.index(identity) if identity in policy.preferred_workers else len(policy.preferred_workers)
                eligible.append(((preference, capabilities.locality != policy.locality, active, identity), worker))
            if not eligible:
                raise WorkerError("no_compatible_worker")
            worker = min(eligible, key=lambda entry: entry[0])[1]
            placement = Placement(process.process_id, process.attempt_id, worker.worker_id, worker.generation, policy.executor)
            connection.execute("INSERT INTO worker_assignments VALUES(?,?,?,?,?,?)", (
                process.process_id, process.attempt_id, worker.worker_id, worker.generation, "reserved", json.dumps(asdict(placement))))
            event = Event(process.process_id, "worker.placed", {"placement": asdict(placement),
                          "locality": policy.locality, "rejected": rejected}, parent_id=process.parent_id)
            connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)", (event.event_id, event.process_id, event.to_json()))
            return placement

    def finish(self, placement: Placement, *, uncertain: bool = False) -> None:
        with self.store._transaction() as connection:
            connection.execute("UPDATE worker_assignments SET state=? WHERE process_id=? AND attempt_id=? AND worker_id=? AND generation=?",
                               ("orphaned" if uncertain else "released", placement.process_id, placement.attempt_id,
                                placement.worker_id, placement.generation))


class DistributedScheduler:
    def __init__(self, kernel: Kernel, placement: WorkerPlacement, transports: Callable[[Worker], JSONTransport]):
        if kernel.records is not placement.store:
            raise ValueError("kernel and placement must share a durable store")
        self.kernel = kernel
        self.placement = placement
        self.transports = transports
        self.assignments: dict[str, Placement] = {}

    async def run(self, process_id: str, policy: WorkerPlacementPolicy) -> Outcome:
        process = self.kernel.processes[process_id]
        self.kernel.authority.require(process_id, Resource.EXECUTOR, "execute", policy.executor)
        placement = self.placement.reserve(process, policy)
        self.assignments[process_id] = placement
        try:
            worker = self.placement.heartbeats.registry.load(placement.worker_id)
            capabilities = self.placement.heartbeats.available()[worker.worker_id]
            features = next(e.features for e in capabilities.executors if e.name == policy.executor)
            executor = RemoteExecutor(worker, policy.executor, self.kernel.workspaces, self.transports(worker),
                                      self.kernel.events.append, features)
            executor.generation_current = lambda: self.placement.heartbeats.registry.load(worker.worker_id).generation == worker.generation
            name = f"remote:{worker.worker_id}:{worker.generation}:{process.attempt_id}"
            self.kernel.executors[name] = executor
            self.kernel.authority.issue(process_id, Resource.EXECUTOR, frozenset({"execute", "control"}), name)
            self.kernel.assignments[process_id] = name
        except Exception:
            self.placement.finish(placement)
            outcome = Outcome(OutcomeStatus.UNAVAILABLE, "remote_placement_setup_failed", retryable=True)
            self.kernel.results[process_id] = outcome
            self.kernel._move(process, State.FAILED)
            self.kernel.budgets.release(process_id)
            self.kernel.events.append(Event(process_id, "process.outcome", json.loads(outcome.to_json()), parent_id=process.parent_id))
            return outcome
        self.kernel.start(process_id)
        try:
            return await asyncio.shield(self.kernel.tasks[process_id])
        finally:
            self.placement.finish(placement, uncertain=process.attempt_id not in executor.results)
