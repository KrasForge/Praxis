"""Executor completion is distinct from verified process success."""

import json
from dataclasses import asdict, dataclass
from enum import Enum

from praxis.kernel.lifecycle import State


class OutcomeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    reason: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    retryable: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.status, OutcomeStatus):
            raise ValueError("invalid outcome status")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("outcome requires machine-readable reason")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported outcome version")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("invalid exit code")
        if self.status == OutcomeStatus.COMPLETED and self.exit_code not in (None, 0):
            raise ValueError("nonzero exit cannot be completed")
        if type(self.retryable) is not bool:
            raise ValueError("invalid retry flag")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("output must be text")

    def process_state(self, *, verified: bool) -> State:
        if self.status == OutcomeStatus.CANCELLED:
            return State.CANCELLED
        if self.status == OutcomeStatus.COMPLETED and verified is True:
            return State.COMPLETED
        return State.FAILED

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Outcome":
        try:
            data = json.loads(raw)
            data["status"] = OutcomeStatus(data["status"])
            return cls(**data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid outcome") from exc
