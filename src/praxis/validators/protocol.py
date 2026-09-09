"""Versioned validator boundary without mutable workspace access."""

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from praxis.kernel.contracts import Check


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ValidationInput:
    process_id: str
    attempt_id: str
    snapshot_id: str
    files: tuple[tuple[str, bytes], ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v for v in (self.process_id, self.attempt_id, self.snapshot_id)):
            raise ValueError("validation lineage required")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported validation input version")
        if not isinstance(self.files, tuple):
            raise ValueError("validation files must be immutable")
        names = set()
        for entry in self.files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("invalid validation file")
            path, content = entry
            parsed = PurePosixPath(path)
            if not isinstance(content, bytes) or parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path or path == ".":
                raise ValueError("invalid validation file")
            if path in names:
                raise ValueError("duplicate validation file")
            names.add(path)


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    reason: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckStatus) or not self.check_id or not self.reason:
            raise ValueError("invalid check result")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported check result version")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CheckResult":
        try:
            data = json.loads(raw)
            data["status"] = CheckStatus(data["status"])
            return cls(**data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid check result") from exc


@runtime_checkable
class Validator(Protocol):
    protocol_version: int

    async def validate(self, source: ValidationInput, check: Check) -> CheckResult: ...


class FakeValidator:
    protocol_version = 1

    def __init__(self, status: CheckStatus = CheckStatus.PASS):
        self.status = status

    async def validate(self, source: ValidationInput, check: Check) -> CheckResult:
        return CheckResult(check.check_id, self.status, "fixture.result")
