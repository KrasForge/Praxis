"""Ordered transactional SQLite layout upgrades; no implicit-commit scripts."""

import sqlite3
from dataclasses import dataclass

from praxis.storage.protocol import StoreError


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(1, (
        "CREATE TABLE IF NOT EXISTS processes (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES processes(id), body TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, process_id TEXT NOT NULL REFERENCES processes(id), body TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS checkpoints (process_id TEXT PRIMARY KEY REFERENCES processes(id), attempt_id TEXT NOT NULL, executor TEXT NOT NULL, protocol_version INTEGER NOT NULL, payload BLOB NOT NULL, snapshot_id TEXT NOT NULL)",
    )),
    Migration(2, (
        "CREATE INDEX IF NOT EXISTS events_process_cursor ON events(process_id,cursor)",
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "INSERT INTO schema_migrations(version) VALUES(1),(2)",
    )),
)
CURRENT_VERSION = 2


def migrate(connection: sqlite3.Connection, migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    if not migrations or any(type(m.version) is not int or m.version != index + 1 for index, m in enumerate(migrations)):
        raise StoreError("invalid_migration_order")
    if connection.in_transaction:
        raise StoreError("migration_requires_idle_connection")
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current < 0 or current > migrations[-1].version:
            raise StoreError("unsupported_store_version")
        for migration in migrations:
            if migration.version > current:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={migration.version}")
        connection.commit()
        return migrations[-1].version
    except Exception:
        connection.rollback()
        raise StoreError("store_migration_failed") from None
