"""Structured process results preserve execution, verification, and evidence semantics."""

import json
from dataclasses import asdict, dataclass, field

from praxis.executors.outcomes import Outcome
from praxis.observability.errors import RuntimeErrorRecord, classify
from praxis.kernel.budgets import RESOURCES
from praxis.kernel.claims import Claim
from praxis.kernel.effects import Effect
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.references import Reference, ReferenceKind
from praxis.validators.policy import VerificationReport
from praxis.validators.protocol import CheckResult


@dataclass(frozen=True)
class ProcessResult:
    process_id: str
    attempt_id: str
    state: State
    outcome: Outcome
    conclusion: str | None = None
    verification: VerificationReport | None = None
    artifacts: tuple[Reference, ...] = ()
    evidence: tuple[Reference, ...] = ()
    claims: tuple[Claim, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    effects: tuple[Effect, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    schema_version: int = 1
    error: RuntimeErrorRecord | None = None

    def __post_init__(self) -> None:
        if not self.process_id or not self.attempt_id or not isinstance(self.state, State) or self.state not in TERMINAL:
            raise ValueError("terminal process identity and state required")
        if not isinstance(self.outcome, Outcome):
            raise ValueError("structured outcome required")
        if self.error is None:
            object.__setattr__(self, "error", classify(self.outcome, self.state))
        elif not isinstance(self.error, RuntimeErrorRecord) or self.state == State.COMPLETED:
            raise ValueError("invalid result error")
        verified = self.verification is not None and self.verification.approved is True
        if self.state != self.outcome.process_state(verified=verified):
            raise ValueError("result_state_outcome_verification_mismatch")
        if self.verification is not None and verified and (self.verification.required_failures or self.verification.missing_outputs or not self.verification.quorum_passed):
            raise ValueError("inconsistent verification report")
        if self.conclusion is not None and not isinstance(self.conclusion, str):
            raise ValueError("invalid conclusion")
        if any(ref.kind != ReferenceKind.ARTIFACT for ref in self.artifacts) or any(ref.kind != ReferenceKind.EVIDENCE for ref in self.evidence):
            raise ValueError("invalid result reference kind")
        for claim in self.claims:
            claim.validate_evidence(self.evidence)
        if any(not isinstance(question, str) for question in self.unresolved_questions):
            raise ValueError("invalid unresolved question")
        if not isinstance(self.usage, dict) or not self.usage.keys() <= RESOURCES or any(type(v) is not int or v < 0 for v in self.usage.values()):
            raise ValueError("invalid result resource usage")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported result version")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "ProcessResult":
        try:
            data = json.loads(raw)
            data["state"] = State(data["state"])
            if data.get("error") is not None:
                data["error"] = RuntimeErrorRecord.from_json(json.dumps(data["error"]))
            data["outcome"] = Outcome.from_json(json.dumps(data["outcome"]))
            for name, model in (("artifacts", Reference), ("evidence", Reference), ("claims", Claim), ("effects", Effect)):
                data[name] = tuple(model.from_json(json.dumps(value)) for value in data.get(name, []))
            data["unresolved_questions"] = tuple(data.get("unresolved_questions", []))
            verification = data.get("verification")
            if verification is not None:
                verification["results"] = tuple(CheckResult.from_json(json.dumps(value)) for value in verification["results"])
                for name in ("required_failures", "advisory_failures", "missing_outputs"):
                    verification[name] = tuple(verification[name])
                data["verification"] = VerificationReport(**verification)
            return cls(**data)
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as exc:
            raise ValueError("invalid process result") from exc
