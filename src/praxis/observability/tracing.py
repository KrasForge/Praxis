"""Deterministic trace/span correlation without a vendor telemetry dependency."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass


def _id(value: str, length: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        for index, (value, size) in enumerate(((self.trace_id, 32), (self.span_id, 16), (self.parent_span_id, 16))):
            if value is None and index == 2:
                continue
            if not isinstance(value, str) or re.fullmatch("[0-9a-f]{" + str(size) + "}", value) is None or set(value) == {"0"}:
                raise ValueError("invalid_trace_context")

    def child(self, kind: str, identity: str) -> "TraceContext":
        return TraceContext(self.trace_id, _id(self.trace_id + ":" + kind + ":" + identity, 16), self.span_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "TraceContext":
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_trace_context") from exc


def process_context(process_id: str, parent_for: Callable[[str], str | None]) -> TraceContext:
    seen = {process_id}
    root = process_id
    parent = parent_for(root)
    while parent is not None:
        if parent in seen:
            raise ValueError("trace_parent_cycle")
        seen.add(parent)
        root = parent
        parent = parent_for(root)
    trace_id = _id("praxis:" + root, 32)
    parent = parent_for(process_id)
    return TraceContext(trace_id, _id(trace_id + ":process:" + process_id, 16),
                        None if parent is None else _id(trace_id + ":process:" + parent, 16))
