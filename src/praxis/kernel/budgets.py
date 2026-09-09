"""Resource limits in exact integer units; None means no configured limit."""

import json
from dataclasses import asdict, dataclass


RESOURCES = frozenset({"tokens", "cost_microusd", "wall_milliseconds", "cpu_milliseconds", "tool_calls"})


@dataclass(frozen=True)
class ResourceBudget:
    tokens: int | None = None
    cost_microusd: int | None = None
    wall_milliseconds: int | None = None
    cpu_milliseconds: int | None = None
    tool_calls: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported budget version")
        for resource in RESOURCES:
            value = getattr(self, resource)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("resource limits must be nonnegative integer units")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ResourceBudget":
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("budget must be an object")
            return cls(**data)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid resource budget") from exc
