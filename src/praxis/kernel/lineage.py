"""Explicit causal lineage across runtime entities."""

import json
from dataclasses import asdict, dataclass

from praxis.kernel.events import Event
from praxis.kernel.process import Process


@dataclass(frozen=True)
class Lineage:
    process_id: str
    attempt_id: str
    caused_by: tuple[str, ...] = ()
    invocation_id: str | None = None
    validator_run_id: str | None = None
    effect_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v for v in (self.process_id, self.attempt_id)):
            raise ValueError("process and attempt lineage required")
        for value in (self.invocation_id, self.validator_run_id, self.effect_id):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("invalid lineage identity")
        for values in (self.caused_by, self.artifact_ids):
            if not isinstance(values, tuple) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError("invalid lineage links")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported lineage version")

    def validate(self, process: Process, events: tuple[Event, ...]) -> None:
        if process.process_id != self.process_id or self.attempt_id not in {entry.attempt_id for entry in process.history}:
            raise ValueError("broken_process_attempt_lineage")
        ids = {event.event_id for event in events}
        if not set(self.caused_by) <= ids:
            raise ValueError("missing_causal_event")
        if self.invocation_id is not None and not any(
            event.type == "executor.invoked" and event.payload.get("invocation_id") == self.invocation_id
            and event.process_id == self.process_id for event in events
        ):
            raise ValueError("missing_executor_invocation")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Lineage":
        try:
            data = json.loads(raw)
            for name in ("caused_by", "artifact_ids"):
                if not isinstance(data.get(name, []), list):
                    raise ValueError("lineage links must be arrays")
                data[name] = tuple(data.get(name, []))
            return cls(**data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid lineage") from exc
