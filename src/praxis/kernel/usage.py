"""Idempotent measured resource usage; absent dimensions are unreported."""

from dataclasses import dataclass

from praxis.kernel.budgets import RESOURCES
from praxis.kernel.events import Event


@dataclass(frozen=True)
class UsageEntry:
    process_id: str
    attempt_id: str
    usage_id: str
    values: tuple[tuple[str, int], ...]


class UsageLedger:
    def __init__(self, events: list[Event]):
        self.events = events
        self.parents: dict[str, str | None] = {}
        self.entries: dict[str, UsageEntry] = {}
        self.attempt_owners: dict[str, str] = {}

    def register(self, process_id: str, parent_id: str | None = None) -> None:
        if process_id in self.parents or parent_id is not None and parent_id not in self.parents:
            raise ValueError("invalid usage lineage")
        self.parents[process_id] = parent_id

    def record(self, process_id: str, attempt_id: str, usage_id: str,
               values: dict[str, int], *, emit: bool = True) -> None:
        if process_id not in self.parents or not attempt_id or not usage_id:
            raise ValueError("usage identity required")
        if not values.keys() <= RESOURCES or any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError("invalid resource usage")
        if self.attempt_owners.get(attempt_id, process_id) != process_id:
            raise ValueError("attempt ownership conflict")
        entry = UsageEntry(process_id, attempt_id, usage_id, tuple(sorted(values.items())))
        previous = self.entries.get(usage_id)
        if previous is not None:
            if previous != entry:
                raise ValueError("usage identity conflict")
            return
        if emit:
            self.events.append(Event(process_id, "usage.recorded", {
                "attempt_id": attempt_id, "usage_id": usage_id, "values": dict(entry.values),
            }, parent_id=self.parents[process_id], event_id=f"usage:{usage_id}"))
        self.attempt_owners[attempt_id] = process_id
        self.entries[usage_id] = entry

    def total(self, process_id: str, *, tree: bool = False, attempt_id: str | None = None) -> dict[str, int]:
        if process_id not in self.parents:
            raise ValueError("unknown process")
        family = {process_id}
        if tree:
            while True:
                expanded = family | {p for p, parent in self.parents.items() if parent in family}
                if expanded == family:
                    break
                family = expanded
        total: dict[str, int] = {}
        for entry in self.entries.values():
            if entry.process_id in family and (attempt_id is None or entry.attempt_id == attempt_id):
                for resource, value in entry.values:
                    total[resource] = total.get(resource, 0) + value
        return total
