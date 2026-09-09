"""Authenticated liveness and versioned worker capability advertisements."""

import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from praxis.executors.features import ExecutorFeatures
from praxis.kernel.process import now
from praxis.remote.workers import WorkerError, WorkerRegistry


@dataclass(frozen=True)
class WorkerCapabilities:
    executors: tuple[ExecutorFeatures, ...]
    workspaces: frozenset[str]
    capacity: int
    locality: str = ""
    features: frozenset[str] = frozenset({"dispatch", "events", "results"})
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.executors, tuple) or not self.executors or any(not isinstance(e, ExecutorFeatures) for e in self.executors):
            raise WorkerError("invalid_worker_executors")
        if len({e.name for e in self.executors}) != len(self.executors):
            raise WorkerError("duplicate_worker_executor")
        if not isinstance(self.workspaces, frozenset) or not self.workspaces or any(not isinstance(w, str) or not w for w in self.workspaces):
            raise WorkerError("invalid_worker_workspaces")
        if type(self.capacity) is not int or self.capacity < 0 or type(self.version) is not int or self.version < 1:
            raise WorkerError("invalid_worker_capacity_version")
        if not isinstance(self.locality, str) or not isinstance(self.features, frozenset) or not self.features <= {"dispatch", "events", "results", "cancel", "reconnect", "checkpoint"}:
            raise WorkerError("invalid_worker_features")

    def to_json(self) -> str:
        return json.dumps({"executors": [json.loads(e.to_json()) for e in self.executors],
                           "workspaces": sorted(self.workspaces), "capacity": self.capacity,
                           "locality": self.locality, "features": sorted(self.features), "version": self.version}, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "WorkerCapabilities":
        try:
            data = json.loads(raw)
            if any(not isinstance(data.get(k), list) for k in ("executors", "workspaces", "features")):
                raise ValueError("capability arrays required")
            data["executors"] = tuple(ExecutorFeatures.from_json(json.dumps(e)) for e in data["executors"])
            data["workspaces"] = frozenset(data["workspaces"])
            data["features"] = frozenset(data["features"])
            return cls(**data)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkerError("invalid_worker_capabilities") from exc


class Heartbeats:
    def __init__(self, registry: WorkerRegistry, *, ttl_seconds: float = 30,
                 clock: Callable[[], float] = time.time):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise WorkerError("invalid_heartbeat_ttl")
        self.registry = registry
        self.clock = clock
        self.ttl = ttl_seconds
        with registry.store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS worker_status (worker_id TEXT PRIMARY KEY, "
                               "generation INTEGER, sequence INTEGER, seen REAL, capabilities TEXT)")

    def heartbeat(self, worker_id: str, generation: int, sequence: int, capabilities: WorkerCapabilities,
                  credential: str) -> None:
        worker = self.registry.load(worker_id)
        try:
            authorized = self.registry.authenticate(credential, worker_id) == worker.principal
        except Exception:
            authorized = False
        if not authorized:
            raise WorkerError("worker_unauthorized")
        if type(generation) is not int or generation != worker.generation:
            raise WorkerError("stale_worker_generation")
        if type(sequence) is not int or sequence < 1:
            raise WorkerError("invalid_heartbeat_sequence")
        raw = capabilities.to_json()
        with self.registry.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.registry.load(worker_id).generation != generation:
                raise WorkerError("stale_worker_generation")
            previous = connection.execute("SELECT generation,sequence,capabilities FROM worker_status WHERE worker_id=?", (worker_id,)).fetchone()
            if previous is not None and previous[0] == generation:
                if sequence <= previous[1]:
                    raise WorkerError("replayed_heartbeat")
                old = WorkerCapabilities.from_json(previous[2])
                if capabilities.version < old.version or (raw != previous[2] and capabilities.version == old.version):
                    raise WorkerError("stale_capability_version")
            connection.execute("INSERT INTO worker_status VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                               "generation=excluded.generation,sequence=excluded.sequence,seen=excluded.seen,capabilities=excluded.capabilities",
                               (worker_id, generation, sequence, self.clock(), raw))
            connection.execute("INSERT INTO worker_events(worker_id,type,timestamp,payload) VALUES(?,?,?,?)",
                               (worker_id, "worker.heartbeat", now(), json.dumps({"generation": generation, "sequence": sequence,
                                                                               "capabilities": json.loads(raw)})))

    def available(self, *, include_saturated: bool = False) -> dict[str, WorkerCapabilities]:
        result = {}
        with self.registry.store._transaction() as connection:
            for identity, generation, seen, raw in connection.execute("SELECT worker_id,generation,seen,capabilities FROM worker_status"):
                age = self.clock() - seen
                if 0 <= age < self.ttl and self.registry.load(identity).generation == generation:
                    capabilities = WorkerCapabilities.from_json(raw)
                    if include_saturated or capabilities.capacity > 0:
                        result[identity] = capabilities
        return result


class RegistryRPC:
    """Registration/heartbeat counterpart hosted through WorkerApplication."""
    def __init__(self, heartbeats: Heartbeats):
        self.heartbeats = heartbeats

    async def rpc(self, data: dict[str, Any], credential: str) -> dict[str, Any]:
        if data.get("operation") == "register":
            worker = self.heartbeats.registry.register(data["worker_id"], data["incarnation"],
                data["protocol_version"], credential, replace_generation=data.get("replace_generation"))
            return {"worker": asdict(worker)}
        if data.get("operation") == "heartbeat":
            self.heartbeats.heartbeat(data["worker_id"], data["generation"], data["sequence"],
                WorkerCapabilities.from_json(json.dumps(data["capabilities"])), credential)
            return {"accepted": True}
        raise WorkerError("unsupported_registry_operation")
