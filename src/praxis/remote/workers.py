"""Durable worker identities with explicit generation replacement."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from praxis.kernel.process import now
from praxis.storage.sqlite import SQLiteStore


class WorkerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Worker:
    worker_id: str
    principal: str
    incarnation: str
    generation: int = 1
    protocol_version: int = 1

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in (
            self.worker_id, self.principal, self.incarnation)):
            raise WorkerError("invalid_worker_identity")
        if type(self.generation) is not int or self.generation < 1:
            raise WorkerError("invalid_worker_generation")
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise WorkerError("incompatible_worker_protocol")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Worker":
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise WorkerError("invalid_worker_record") from exc


class WorkerRegistry:
    def __init__(self, store: SQLiteStore, authenticate: Callable[[str, str], str | None]):
        self.store = store
        self.authenticate = authenticate
        with store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, generation INTEGER, body TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS worker_events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, "
                               "worker_id TEXT, type TEXT, timestamp TEXT, payload TEXT)")

    def register(self, worker_id: str, incarnation: str, protocol_version: int, credential: str,
                 *, replace_generation: int | None = None) -> Worker:
        try:
            principal = self.authenticate(credential, worker_id)
        except Exception:
            principal = None
        if not principal:
            raise WorkerError("worker_unauthorized")
        worker = Worker(worker_id, principal, incarnation, protocol_version=protocol_version)
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT body FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is not None:
                previous = Worker.from_json(row[0])
                if previous.principal != principal:
                    raise WorkerError("worker_owner_mismatch")
                if previous.incarnation == incarnation and previous.protocol_version == protocol_version:
                    return previous
                if type(replace_generation) is not int or replace_generation != previous.generation:
                    raise WorkerError("worker_identity_conflict")
                worker = Worker(worker_id, principal, incarnation, previous.generation + 1, protocol_version)
            elif replace_generation is not None:
                raise WorkerError("worker_not_found")
            connection.execute("INSERT INTO workers VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,body=excluded.body",
                               (worker_id, worker.generation, worker.to_json()))
            connection.execute("INSERT INTO worker_events(worker_id,type,timestamp,payload) VALUES(?,?,?,?)",
                               (worker_id, "worker.registered", now(), worker.to_json()))
        return worker

    def load(self, worker_id: str) -> Worker:
        with self.store._transaction() as connection:
            row = connection.execute("SELECT body FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                raise WorkerError("worker_not_found")
            worker = Worker.from_json(row[0])
            if worker.worker_id != worker_id:
                raise WorkerError("worker_identity_corrupt")
            return worker

    def events(self, worker_id: str) -> tuple[dict[str, Any], ...]:
        with self.store._transaction() as connection:
            return tuple({"cursor": cursor, "type": kind, "timestamp": timestamp, "payload": json.loads(raw)}
                         for cursor, kind, timestamp, raw in connection.execute(
                             "SELECT cursor,type,timestamp,payload FROM worker_events WHERE worker_id=? ORDER BY cursor", (worker_id,)))
