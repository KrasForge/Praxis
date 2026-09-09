"""Durable process store contract with atomic process/event writes."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from praxis.executors.protocol import Checkpoint
from praxis.kernel.events import Event
from praxis.kernel.process import Process


class StoreError(ValueError):
    code = "store_error"


class StoreConflict(StoreError):
    code = "store_conflict"


@dataclass(frozen=True)
class StoredEvent:
    cursor: int
    event: Event


@dataclass(frozen=True)
class StoredCheckpoint:
    checkpoint: Checkpoint
    workspace_snapshot_id: str


@runtime_checkable
class ProcessStore(Protocol):
    def save(self, process: Process, events: tuple[Event, ...] = ()) -> None: ...
    def load(self, process_id: str) -> Process: ...
    def list_processes(self) -> tuple[str, ...]: ...
    def read_events(self, process_id: str | None = None, *, after: int = 0) -> tuple[StoredEvent, ...]: ...
    def save_checkpoint(self, checkpoint: StoredCheckpoint) -> None: ...
    def load_checkpoint(self, process_id: str) -> StoredCheckpoint | None: ...
    def close(self) -> None: ...
