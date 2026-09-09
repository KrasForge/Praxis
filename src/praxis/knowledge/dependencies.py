"""Validated context dependencies declared by a ProcessSpec."""

import json
from dataclasses import dataclass
from typing import Any

from praxis.knowledge.context import ContextRequest


class RequiredContextUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ContextDependency:
    context_id: str
    provider: str
    request: ContextRequest
    required: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextDependency":
        if not isinstance(data, dict) or set(data) - {"context_id", "provider", "request", "required"}:
            raise ValueError("invalid context dependency")
        if any(not isinstance(data.get(k), str) or not data[k] for k in ("context_id", "provider")):
            raise ValueError("context identity and provider required")
        if type(data.get("required", True)) is not bool:
            raise ValueError("context required must be boolean")
        return cls(data["context_id"], data["provider"],
                   ContextRequest.from_json(json.dumps(data.get("request"))), data.get("required", True))
