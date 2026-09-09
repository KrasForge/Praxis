"""Read-only process ancestry and ordered causal history over durable records."""

from praxis.kernel.lineage import Lineage
from praxis.storage.protocol import ProcessStore, StoredEvent


class ProvenanceError(ValueError):
    code = "provenance_integrity_error"


class ProvenanceQueries:
    def __init__(self, store: ProcessStore):
        self.store = store

    def ancestors(self, process_id: str) -> tuple[str, ...]:
        known = set(self.store.list_processes())
        if process_id not in known:
            raise ProvenanceError("process_not_found")
        visited = {process_id}
        result = []
        parent = self.store.load(process_id).parent_id
        while parent is not None:
            if parent not in known or parent in visited:
                raise ProvenanceError("broken_parent_lineage")
            visited.add(parent)
            result.append(parent)
            parent = self.store.load(parent).parent_id
        return tuple(reversed(result))

    def descendants(self, process_id: str) -> tuple[str, ...]:
        self.ancestors(process_id)
        records = {identity: self.store.load(identity) for identity in self.store.list_processes()}
        result = []
        pending = [process_id]
        visited = {process_id}
        while pending:
            parent = pending.pop(0)
            children = sorted(identity for identity, record in records.items() if record.parent_id == parent)
            for child in children:
                if child in visited:
                    raise ProvenanceError("process_lineage_cycle")
                visited.add(child)
                result.append(child)
                pending.append(child)
        return tuple(result)

    def causal_sequence(self, process_id: str, *, tree: bool = True) -> tuple[StoredEvent, ...]:
        family = {process_id}
        if tree:
            family.update(self.descendants(process_id))
        else:
            self.ancestors(process_id)
        events = self.store.read_events()
        by_id = {entry.event.event_id: entry for entry in events}
        selected = tuple(entry for entry in events if entry.event.process_id in family)
        for identity in family:
            if not any(entry.event.type == "process.created" and entry.event.process_id == identity for entry in selected):
                raise ProvenanceError("missing_process_creation")
        for entry in selected:
            event = entry.event
            process = self.store.load(event.process_id)
            if event.parent_id != process.parent_id:
                raise ProvenanceError("event_parent_mismatch")
            raw = event.payload.get("lineage")
            required = event.type in ("executor.invoked", "contract.checked") or event.type.startswith("effect.")
            if raw is None:
                if required:
                    raise ProvenanceError("missing_required_lineage")
                continue
            try:
                lineage = Lineage.from_json(raw)
                lineage.validate(process, tuple(item.event for item in events))
                if any(by_id[cause].cursor >= entry.cursor for cause in lineage.caused_by):
                    raise ValueError("causal_order_violation")
            except (TypeError, ValueError, KeyError) as exc:
                raise ProvenanceError("broken_causal_lineage") from exc
        return selected
