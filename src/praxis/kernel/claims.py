"""Claims retain supporting and contradicting evidence without inventing support."""

import json
import math
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from praxis.kernel.references import Reference, ReferenceKind


@dataclass(frozen=True)
class Claim:
    statement: str
    process_id: str
    attempt_id: str
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    confidence: float | None = None
    claim_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value in (self.statement, self.process_id, self.attempt_id, self.claim_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("claim statement and provenance required")
        for refs in (self.supporting, self.contradicting):
            if not isinstance(refs, tuple) or any(not isinstance(ref, str) or not ref for ref in refs) or len(set(refs)) != len(refs):
                raise ValueError("invalid claim evidence references")
        if self.confidence is not None and (type(self.confidence) not in (float, int) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1):
            raise ValueError("confidence must be between zero and one")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported claim version")

    @property
    def unsupported(self) -> bool:
        return not self.supporting

    def validate_evidence(self, evidence: tuple[Reference, ...]) -> None:
        ids = {ref.reference_id for ref in evidence if ref.kind == ReferenceKind.EVIDENCE}
        if not set(self.supporting + self.contradicting) <= ids:
            raise ValueError("missing_claim_evidence")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Claim":
        try:
            data = json.loads(raw)
            for name in ("supporting", "contradicting"):
                if not isinstance(data.get(name, []), list):
                    raise ValueError("evidence references must be arrays")
                data[name] = tuple(data.get(name, []))
            return cls(**data)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid claim") from exc
