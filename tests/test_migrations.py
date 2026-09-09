import sqlite3

import pytest

from praxis.kernel.events import Event
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.migrations import CURRENT_VERSION, MIGRATIONS, Migration, migrate
from praxis.storage.protocol import StoreError
from praxis.storage.sqlite import SQLiteStore


def historical(path):
    connection = sqlite3.connect(path)
    migrate(connection, MIGRATIONS[:1])
    process = Process(ProcessSpec("historical", "fake"))
    event = Event(process.process_id, "process.created")
    connection.execute("INSERT INTO processes VALUES(?,?,?)", (process.process_id, None, process.to_json()))
    connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)", (event.event_id, process.process_id, event.to_json()))
    connection.commit()
    return connection, process, event


def test_upgrade_preserves_records_and_cursors(tmp_path):
    connection, process, event = historical(tmp_path / "db")
    connection.close()
    store = SQLiteStore(tmp_path / "db")
    assert store.load(process.process_id).to_json() == process.to_json()
    assert store.read_events()[0].cursor == 1 and store.read_events()[0].event == event
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    assert store.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
    store.close()
    SQLiteStore(tmp_path / "db").close()


def test_failed_upgrade_rolls_back_schema_and_data(tmp_path):
    connection, process, event = historical(tmp_path / "db")
    broken = (*MIGRATIONS[:1], Migration(2, ("DELETE FROM events", "CREATE TABLE temporary_change(x)", "INVALID SQL")))
    with pytest.raises(StoreError, match="migration_failed"):
        migrate(connection, broken)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("SELECT body FROM events").fetchone()[0] == event.to_json()
    assert not connection.execute("SELECT name FROM sqlite_master WHERE name='temporary_change'").fetchall()
    assert migrate(connection) == 2
    connection.close()


def test_future_and_unordered_layouts_rejected():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA user_version=99")
    with pytest.raises(StoreError):
        migrate(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    with pytest.raises(StoreError, match="order"):
        migrate(connection, (Migration(2, ()),))
    connection.close()
