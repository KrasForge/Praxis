"""SQLite process storage using atomic transactions, WAL, and full synchronization."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from praxis.executors.protocol import Checkpoint
from praxis.kernel.events import Event
from praxis.kernel.process import Process
from praxis.storage.protocol import StoredCheckpoint, StoredEvent, StoreConflict, StoreError


class SQLiteStore:
    def __init__(self, path: Path):
        self.lock = RLock()
        self.closed = False
        try:
            self.connection = sqlite3.connect(path, check_same_thread=False)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StoreError("unsupported_store_version")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS processes (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES processes(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    process_id TEXT NOT NULL REFERENCES processes(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    process_id TEXT PRIMARY KEY REFERENCES processes(id), attempt_id TEXT NOT NULL,
                    executor TEXT NOT NULL, protocol_version INTEGER NOT NULL,
                    payload BLOB NOT NULL, snapshot_id TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
        except sqlite3.DatabaseError as exc:
            raise StoreError("cannot_open_store") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            if self.closed:
                raise StoreError("store_closed")
            try:
                with self.connection:
                    yield self.connection
            except sqlite3.DatabaseError as exc:
                raise StoreError("database_error") from exc

    def save(self, process: Process, events: tuple[Event, ...] = ()) -> None:
        raw = process.to_json()
        Process.from_json(raw)
        with self._transaction() as connection:
            connection.execute("INSERT INTO processes(id,parent_id,body) VALUES(?,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,body=excluded.body",
                               (process.process_id, process.parent_id, raw))
            for event in events:
                if event.process_id != process.process_id or event.parent_id != process.parent_id:
                    raise StoreError("event_lineage_mismatch")
                body = event.to_json()
                existing = connection.execute("SELECT body FROM events WHERE event_id=?", (event.event_id,)).fetchone()
                if existing is not None:
                    if existing[0] != body:
                        raise StoreConflict("event_identity_conflict")
                else:
                    connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)",
                                       (event.event_id, event.process_id, body))

    def load(self, process_id: str) -> Process:
        with self._transaction() as connection:
            row = connection.execute("SELECT body FROM processes WHERE id=?", (process_id,)).fetchone()
            if row is None:
                raise StoreError("process_not_found")
            try:
                process = Process.from_json(row[0])
                if process.process_id != process_id:
                    raise ValueError("identity mismatch")
                return process
            except ValueError as exc:
                raise StoreError("corrupt_process_record") from exc

    def list_processes(self) -> tuple[str, ...]:
        with self._transaction() as connection:
            return tuple(row[0] for row in connection.execute("SELECT id FROM processes ORDER BY id"))

    def read_events(self, process_id: str | None = None, *, after: int = 0) -> tuple[StoredEvent, ...]:
        with self._transaction() as connection:
            rows = connection.execute("SELECT cursor,body FROM events WHERE cursor>? "
                                      "AND (? IS NULL OR process_id=?) ORDER BY cursor", (after, process_id, process_id))
            try:
                return tuple(StoredEvent(row[0], Event.from_json(row[1])) for row in rows)
            except ValueError as exc:
                raise StoreError("corrupt_event_record") from exc

    def save_checkpoint(self, checkpoint: StoredCheckpoint) -> None:
        item = checkpoint.checkpoint
        with self._transaction() as connection:
            row = connection.execute("SELECT body FROM processes WHERE id=?", (item.process_id,)).fetchone()
            if row is None or Process.from_json(row[0]).attempt_id != item.attempt_id:
                raise StoreError("checkpoint_attempt_mismatch")
            connection.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?) "
                               "ON CONFLICT(process_id) DO UPDATE SET attempt_id=excluded.attempt_id,"
                               "executor=excluded.executor,protocol_version=excluded.protocol_version,"
                               "payload=excluded.payload,snapshot_id=excluded.snapshot_id",
                               (item.process_id, item.attempt_id, item.executor, item.protocol_version,
                                item.payload, checkpoint.workspace_snapshot_id))

    def load_checkpoint(self, process_id: str) -> StoredCheckpoint | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT executor,process_id,attempt_id,payload,protocol_version,snapshot_id "
                                      "FROM checkpoints WHERE process_id=?", (process_id,)).fetchone()
            return None if row is None else StoredCheckpoint(Checkpoint(*row[:5]), row[5])

    def close(self) -> None:
        with self.lock:
            self.connection.close()
            self.closed = True
