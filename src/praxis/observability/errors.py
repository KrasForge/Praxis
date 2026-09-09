"""Versioned failure categories and bounded causal trees; never exception text."""

import json
from dataclasses import asdict, dataclass
from enum import Enum

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.lifecycle import State


class ErrorCode(str, Enum):
    TRANSPORT = "transport_unavailable"
    EXECUTOR = "executor_failed"
    CONTRACT = "contract_rejected"
    POLICY = "policy_denied"
    BUDGET = "budget_exhausted"
    CANCELLED = "execution_cancelled"


@dataclass(frozen=True)
class RuntimeErrorRecord:
    code: ErrorCode
    causes: tuple["RuntimeErrorRecord", ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode) or type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("invalid runtime error")
        if not isinstance(self.causes, tuple) or any(not isinstance(c, RuntimeErrorRecord) for c in self.causes):
            raise ValueError("invalid error causes")
        pending = [(self, 0)]
        count = 0
        while pending:
            record, depth = pending.pop()
            count += 1
            if depth > 16 or count > 256:
                raise ValueError("error chain limit")
            pending.extend((cause, depth + 1) for cause in record.causes)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "RuntimeErrorRecord":
        def parse(data: object, depth: int) -> "RuntimeErrorRecord":
            if depth > 16 or not isinstance(data, dict) or not data.keys() <= {"code", "causes", "schema_version"}:
                raise ValueError("invalid error chain")
            causes = data.get("causes", [])
            if not isinstance(causes, list):
                raise ValueError("invalid error causes")
            return cls(ErrorCode(data["code"]), tuple(parse(c, depth + 1) for c in causes), data.get("schema_version", 1))
        try:
            if len(raw) > 65536:
                raise ValueError("error chain limit")
            return parse(json.loads(raw), 0)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ValueError("invalid runtime error") from exc


def classify(outcome: Outcome, state: State) -> RuntimeErrorRecord | None:
    if state == State.COMPLETED:
        return None
    if outcome.status == OutcomeStatus.COMPLETED:
        code = ErrorCode.CONTRACT
    elif "transport" in outcome.reason or "connection" in outcome.reason:
        code = ErrorCode.TRANSPORT
    elif "denied" in outcome.reason or "capability" in outcome.reason or "policy" in outcome.reason:
        code = ErrorCode.POLICY
    elif outcome.status == OutcomeStatus.BUDGET_EXHAUSTED:
        code = ErrorCode.BUDGET
    elif outcome.status == OutcomeStatus.CANCELLED:
        code = ErrorCode.CANCELLED
    else:
        code = ErrorCode.EXECUTOR
    return RuntimeErrorRecord(code)
