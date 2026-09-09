"""In-memory conformance backend; intentionally not durable."""

from threading import RLock

from praxis.kernel.events import Event
from praxis.kernel.process import Process
from praxis.storage.protocol import StoredCheckpoint, StoredEvent, StoreConflict, StoreError


class MemoryStore:
    def __init__(self) -> None:
        self.processes: dict[str, str] = {}
        self.events: list[StoredEvent] = []
        self.checkpoints: dict[str, StoredCheckpoint] = {}
        self.lock = RLock()
        self.closed = False

    def _open(self) -> None:
        if self.closed:
            raise StoreError("store_closed")

    def save(self, process: Process, events: tuple[Event, ...] = ()) -> None:
        raw = process.to_json()
        Process.from_json(raw)
        with self.lock:
            self._open()
            if process.parent_id is not None and process.parent_id not in self.processes:
                raise StoreError("missing_parent")
            known = {entry.event.event_id: entry.event.to_json() for entry in self.events}
            pending = []
            for event in events:
                serialized = event.to_json()
                if event.process_id != process.process_id or event.parent_id != process.parent_id:
                    raise StoreError("event_lineage_mismatch")
                if event.event_id in known and known[event.event_id] != serialized:
                    raise StoreConflict("event_identity_conflict")
                if event.event_id not in known:
                    pending.append(Event.from_json(serialized))
                    known[event.event_id] = serialized
            self.processes[process.process_id] = raw
            for event in pending:
                self.events.append(StoredEvent(len(self.events) + 1, event))

    def load(self, process_id: str) -> Process:
        with self.lock:
            self._open()
            if process_id not in self.processes:
                raise StoreError("process_not_found")
            return Process.from_json(self.processes[process_id])

    def list_processes(self) -> tuple[str, ...]:
        with self.lock:
            self._open()
            return tuple(sorted(self.processes))

    def read_events(self, process_id: str | None = None, *, after: int = 0) -> tuple[StoredEvent, ...]:
        with self.lock:
            self._open()
            return tuple(StoredEvent(e.cursor, Event.from_json(e.event.to_json())) for e in self.events
                         if e.cursor > after and (process_id is None or e.event.process_id == process_id))

    def save_checkpoint(self, checkpoint: StoredCheckpoint) -> None:
        with self.lock:
            process = self.load(checkpoint.checkpoint.process_id)
            if process.attempt_id != checkpoint.checkpoint.attempt_id:
                raise StoreError("checkpoint_attempt_mismatch")
            self.checkpoints[process.process_id] = checkpoint

    def load_checkpoint(self, process_id: str) -> StoredCheckpoint | None:
        with self.lock:
            self._open()
            return self.checkpoints.get(process_id)

    def close(self) -> None:
        self.closed = True
