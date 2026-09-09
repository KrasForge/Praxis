"""List-compatible audit sink backed by durable, idempotent event writes."""

from praxis.kernel.events import Event
from praxis.storage.protocol import ProcessStore


class EventJournal(list[Event]):
    def __init__(self, store: ProcessStore):
        super().__init__(entry.event for entry in store.read_events())
        self.store = store

    def append(self, event: Event) -> None:
        process = self.store.load(event.process_id)
        # Authority events may omit the process parent; bind trusted audit lineage.
        if event.parent_id != process.parent_id:
            from dataclasses import replace
            event = replace(event, parent_id=process.parent_id)
        self.store.save(process, (event,))
        if all(existing.event_id != event.event_id for existing in self):
            super().append(event)
