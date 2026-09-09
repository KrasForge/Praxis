import json

import pytest

from praxis.kernel.events import Event, EventError


def test_roundtrip():
    event = Event("process-1", "process.created", {"nested": [1, None, True]})
    assert Event.from_json(event.to_json()) == event


@pytest.mark.parametrize("field,value", [
    ("process_id", ""), ("parent_id", 3), ("schema_version", 2),
    ("schema_version", True), ("timestamp", "2026-01-01"),
    ("payload", []), ("payload", {"bad": float("nan")}), ("unknown", 1),
])
def test_reject_envelope(field, value):
    data = json.loads(Event("p", "created").to_json())
    data[field] = value
    with pytest.raises(EventError):
        Event.from_json(json.dumps(data))


@pytest.mark.parametrize("raw", ["null", "[]", "{", "{}"])
def test_invalid_json(raw):
    with pytest.raises(EventError):
        Event.from_json(raw)
