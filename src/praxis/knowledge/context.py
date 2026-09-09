"""Provider-neutral bounded context retrieval contract."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from praxis.kernel.events import _validate_json


@dataclass(frozen=True)
class ContextRequest:
    query: str
    filters: tuple[tuple[str, str], ...] = ()
    newer_than: str | None = None
    max_results: int = 10
    max_bytes: int = 65536

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("context query required")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 100:
            raise ValueError("context result bound must be 1..100")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 1048576:
            raise ValueError("context byte bound must be 1..1048576")
        if not isinstance(self.filters, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
            or any(not isinstance(v, str) or not v for v in pair) for pair in self.filters):
            raise ValueError("invalid context filters")
        if len(dict(self.filters)) != len(self.filters):
            raise ValueError("duplicate context filters")
        if self.newer_than is not None and datetime.fromisoformat(self.newer_than).utcoffset() is None:
            raise ValueError("aware context freshness required")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ContextRequest":
        try:
            data = json.loads(raw)
            data["filters"] = tuple(tuple(pair) for pair in data.get("filters", []))
            return cls(**data)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("invalid context request") from exc


@dataclass(frozen=True)
class ContextItem:
    source_id: str
    text: str
    observed_at: str
    provenance_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id or not isinstance(self.text, str):
            raise ValueError("invalid context item")
        if datetime.fromisoformat(self.observed_at).utcoffset() is None:
            raise ValueError("aware context timestamp required")
        provenance = json.loads(self.provenance_json)
        if not isinstance(provenance, dict):
            raise ValueError("context provenance required")
        _validate_json(provenance)


@dataclass(frozen=True)
class ContextResponse:
    status: str
    items: tuple[ContextItem, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"available", "partial", "unavailable"}:
            raise ValueError("invalid context status")
        if not isinstance(self.items, tuple) or any(not isinstance(item, ContextItem) for item in self.items):
            raise ValueError("immutable context items required")
        if self.status == "unavailable" and self.items:
            raise ValueError("unavailable context cannot contain items")


class ContextProvider(Protocol):
    async def query(self, request: ContextRequest) -> ContextResponse: ...


class FakeContextProvider:
    def __init__(self, response: ContextResponse):
        self.response = response

    async def query(self, request: ContextRequest) -> ContextResponse:
        return bound_response(request, self.response)


def bound_response(request: ContextRequest, response: ContextResponse) -> ContextResponse:
    if response.status == "unavailable":
        return response
    selected: list[ContextItem] = []
    size = 0
    for item in response.items:
        if request.newer_than and datetime.fromisoformat(item.observed_at) < datetime.fromisoformat(request.newer_than):
            continue
        item_size = len(json.dumps(asdict(item), ensure_ascii=False).encode())
        if size + item_size > request.max_bytes or len(selected) >= request.max_results:
            continue
        selected.append(item)
        size += item_size
    truncated = len(selected) != len(response.items)
    return ContextResponse("partial" if truncated else response.status, tuple(selected),
                           "context_bounds_applied" if truncated else response.reason)
