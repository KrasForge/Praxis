"""Versioned speculative comparison identities and lifecycle."""

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from uuid import uuid4


class CandidateState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    EVALUATING = "evaluating"
    SELECTED = "selected"
    FAILED = "failed"


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    process_id: str

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v for v in (self.candidate_id, self.process_id)):
            raise ValueError("candidate identity required")


@dataclass(frozen=True)
class CandidateGroup:
    parent_id: str
    objective: str
    candidates: tuple[Candidate, ...]
    group_id: str = field(default_factory=lambda: str(uuid4()))
    state: CandidateState = CandidateState.PENDING
    selected_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() for v in (self.parent_id, self.objective, self.group_id)):
            raise ValueError("group identity and objective required")
        if not isinstance(self.candidates, tuple) or not self.candidates or any(
            not isinstance(c, Candidate) for c in self.candidates
        ):
            raise ValueError("candidates required")
        if len({c.candidate_id for c in self.candidates}) != len(self.candidates) or len({
            c.process_id for c in self.candidates
        }) != len(self.candidates):
            raise ValueError("duplicate candidate")
        if not isinstance(self.state, CandidateState):
            raise ValueError("invalid group state")
        if self.state == CandidateState.SELECTED:
            if self.selected_id not in {c.candidate_id for c in self.candidates}:
                raise ValueError("selection must name candidate")
        elif self.selected_id is not None:
            raise ValueError("selection requires selected state")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported candidate version")

    def move(self, state: CandidateState, selected_id: str | None = None) -> "CandidateGroup":
        allowed = {
            CandidateState.PENDING: {CandidateState.RUNNING, CandidateState.FAILED},
            CandidateState.RUNNING: {CandidateState.EVALUATING, CandidateState.FAILED},
            CandidateState.EVALUATING: {CandidateState.SELECTED, CandidateState.FAILED},
            CandidateState.SELECTED: set(), CandidateState.FAILED: set(),
        }
        if state not in allowed[self.state]:
            raise ValueError("invalid candidate transition")
        return replace(self, state=state, selected_id=selected_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CandidateGroup":
        try:
            data = json.loads(raw)
            data["candidates"] = tuple(Candidate(**c) for c in data["candidates"])
            data["state"] = CandidateState(data["state"])
            return cls(**data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid candidate group") from exc
