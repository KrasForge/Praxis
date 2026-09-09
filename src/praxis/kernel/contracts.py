"""Machine-checkable process acceptance contracts."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from praxis.kernel.events import _validate_json


@dataclass(frozen=True)
class Check:
    check_id: str
    validator: str
    required: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() for v in (self.check_id, self.validator)):
            raise ValueError("check identity and validator required")
        if type(self.required) is not bool or not isinstance(self.parameters, dict):
            raise ValueError("invalid check configuration")
        _validate_json(self.parameters)


@dataclass(frozen=True)
class Contract:
    required_outputs: tuple[str, ...] = ()
    invariants: tuple[Check, ...] = ()
    validators: tuple[Check, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported contract version")
        for path in self.required_outputs:
            if not isinstance(path, str) or not path or "\x00" in path:
                raise ValueError("invalid required output")
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path or path == ".":
                raise ValueError("outputs must be normalized workspace-relative paths")
        checks = self.invariants + self.validators
        if any(not isinstance(check, Check) for check in checks):
            raise ValueError("invalid validator check")
        ids = {check.check_id for check in checks}
        if len(ids) != len(checks) or len(set(self.required_outputs)) != len(self.required_outputs):
            raise ValueError("duplicate contract identity")
        if not set(self.acceptance_checks) <= ids:
            raise ValueError("unknown acceptance check")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Contract":
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("contract must be an object")
            for name in ("required_outputs", "invariants", "validators", "acceptance_checks"):
                values = data.get(name, [])
                if not isinstance(values, list):
                    raise ValueError("contract collections must be arrays")
                data[name] = tuple(Check(**value) for value in values) if name in ("invariants", "validators") else tuple(values)
            return cls(**data)
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise ValueError("invalid contract") from exc
