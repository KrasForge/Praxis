"""Bounded strict JSON at public trust boundaries."""

import json
import math
from typing import Any


def validate_json(value: Any) -> None:
    pending = [(value, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if depth > 64 or count > 100000:
            raise ValueError("JSON complexity limit")
        if isinstance(current, dict):
            if any(not isinstance(k, str) for k in current):
                raise ValueError("JSON keys must be strings")
            pending.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            pending.extend((v, depth + 1) for v in current)
        elif current is not None and type(current) not in (str, int, float, bool):
            raise ValueError("invalid JSON value")
        elif type(current) is float and not math.isfinite(current):
            raise ValueError("nonfinite JSON value")


def load_object(raw: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    try:
        if not isinstance(raw, str) or len(raw) > 16777216:
            raise ValueError("JSON size limit")
        value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        validate_json(value)
        return value
    except (RecursionError, TypeError, ValueError):
        raise ValueError("invalid JSON document") from None
