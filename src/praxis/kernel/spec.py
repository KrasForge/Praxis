"""Versioned, vendor-independent process submission schema."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from praxis.kernel.budgets import ResourceBudget
from praxis.kernel.contracts import Contract
from praxis.kernel.events import EventError, _validate_json


class SpecError(ValueError):
    code = "invalid_process_spec"

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{self.code}: {field}: {reason}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "reason": self.reason}


@dataclass(frozen=True)
class ProcessSpec:
    objective: str
    executor: str
    inputs: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    contract: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    deadline: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("objective", "executor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SpecError(name, "required nonempty string")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SpecError("schema_version", "unsupported version")
        for name in ("inputs", "environment", "contract", "budget", "metadata"):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise SpecError(name, "expected object")
            self._json(name, value)
        if type(self.priority) is not int:
            raise SpecError("priority", "expected integer")
        try:
            ResourceBudget.from_json(json.dumps(self.budget))
        except ValueError:
            raise SpecError("budget", "invalid resource budget") from None
        try:
            Contract.from_json(json.dumps(self.contract))
        except ValueError:
            raise SpecError("contract", "invalid contract") from None
        if any(not isinstance(v, str) for v in self.environment.values()):
            raise SpecError("environment", "expected string values")
        if not isinstance(self.capabilities, list) or any(
            not isinstance(item, dict) for item in self.capabilities
        ):
            raise SpecError("capabilities", "expected array of objects")
        self._json("capabilities", self.capabilities)
        if self.deadline is not None:
            try:
                if datetime.fromisoformat(self.deadline).utcoffset() is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise SpecError("deadline", "expected timezone-aware timestamp") from None

    @staticmethod
    def _json(name: str, value: Any) -> None:
        try:
            _validate_json(value)
        except (EventError, RecursionError):
            raise SpecError(name, "expected finite JSON values") from None

    def to_json(self) -> str:
        self.__post_init__()
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "ProcessSpec":
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise SpecError("schema", "expected object")
            return cls(**data)
        except SpecError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise SpecError("schema", "malformed, missing, or unknown fields") from None
