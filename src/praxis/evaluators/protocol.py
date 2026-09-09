"""Evaluation receives serialized snapshots, never mutable live process state."""

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Protocol
from uuid import uuid4

from praxis.kernel.events import _validate_json
from praxis.kernel.results import ProcessResult


@dataclass(frozen=True)
class EvaluationInput:
    group_id: str
    candidates: tuple[tuple[str, str], ...]
    rubric_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id:
            raise ValueError("group identity required")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ValueError("immutable candidates required")
        ids = []
        for pair in self.candidates:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("immutable candidate pair required")
            identity, raw = pair
            if not isinstance(identity, str) or not identity:
                raise ValueError("candidate identity required")
            ids.append(identity)
            ProcessResult.from_json(raw)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate")
        rubric = json.loads(self.rubric_json)
        if not isinstance(rubric, dict):
            raise ValueError("rubric must be an object")
        _validate_json(rubric)

    def results(self) -> tuple[tuple[str, ProcessResult], ...]:
        return tuple((identity, ProcessResult.from_json(raw)) for identity, raw in self.candidates)


@dataclass(frozen=True)
class Evaluation:
    evaluator_id: str
    status: str
    scores: tuple[tuple[str, float], ...] = ()
    comparisons: tuple[tuple[str, str, int], ...] = ()
    rationale: str = ""
    evaluation_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.evaluator_id or not self.evaluation_id or self.status not in {"ok", "error", "unavailable"}:
            raise ValueError("invalid evaluation")
        if not isinstance(self.scores, tuple) or not isinstance(self.comparisons, tuple):
            raise ValueError("immutable evaluation required")
        ids = []
        for pair in self.scores:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid score")
            identity, score = pair
            if not isinstance(identity, str) or not identity or type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("invalid score")
            ids.append(identity)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate score")
        for comparison in self.comparisons:
            if not isinstance(comparison, tuple) or len(comparison) != 3:
                raise ValueError("invalid comparison")
            left, right, order = comparison
            if not left or not right or left == right or type(order) is not int or order not in {-1, 0, 1}:
                raise ValueError("invalid comparison")
        if self.status != "ok" and (self.scores or self.comparisons):
            raise ValueError("failed evaluation cannot vote")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class Evaluator(Protocol):
    @property
    def name(self) -> str: ...
    async def evaluate(self, request: EvaluationInput) -> Evaluation: ...


class FakeEvaluator:
    def __init__(self, result: Evaluation):
        self.result = result
        self.name = result.evaluator_id

    async def evaluate(self, request: EvaluationInput) -> Evaluation:
        return self.result


async def run_evaluator(evaluator: Evaluator, request: EvaluationInput) -> Evaluation:
    try:
        result = await evaluator.evaluate(request)
        candidates = {identity for identity, _ in request.candidates}
        referenced = {identity for identity, _ in result.scores} | {
            identity for left, right, _ in result.comparisons for identity in (left, right)}
        if result.evaluator_id != evaluator.name or not referenced <= candidates:
            raise ValueError("evaluation identity mismatch")
        return result
    except Exception:
        return Evaluation(evaluator.name, "error", rationale="evaluator_failed")
