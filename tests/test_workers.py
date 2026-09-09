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
