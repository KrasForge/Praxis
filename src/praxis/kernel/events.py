"""Version-one JSON event envelope; unknown fields fail closed."""

import json
from praxis.kernel.parsing import load_object
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class EventError(ValueError):
    code = "invalid_event"


@dataclass(frozen=True)
class Event:
    process_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value in (self.process_id, self.type, self.event_id):
            if not isinstance(value, str) or not value.strip():
                raise EventError("identity and type must be nonempty strings")
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id.strip()
        ):
            raise EventError("invalid parent identity")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise EventError("unsupported event version")
        try:
            timestamp = datetime.fromisoformat(self.timestamp)
            if timestamp.utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise EventError("timestamp must be timezone-aware ISO 8601") from None
        if not isinstance(self.payload, dict):
            raise EventError("payload must be an object")
        _validate_json(self.payload)

    def to_json(self) -> str:
        _validate_json(self.payload)
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        try:
            data = load_object(raw)
            if not isinstance(data, dict):
                raise EventError("envelope must be an object")
            required = {"process_id", "type", "payload", "parent_id", "event_id",
                        "timestamp", "schema_version"}
            if set(data) != required:
                raise EventError("missing or unknown envelope fields")
            return cls(**data)
        except (TypeError, ValueError, RecursionError) as exc:
            raise EventError("invalid event envelope") from exc


def _validate_json(value: Any) -> None:
    from praxis.kernel.parsing import validate_json
    try:
        validate_json(value)
    except ValueError:
        raise EventError("invalid JSON value") from None
