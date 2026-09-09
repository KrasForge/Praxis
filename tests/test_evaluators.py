import asyncio

import pytest

from praxis.evaluators.protocol import Evaluation, EvaluationInput, FakeEvaluator, run_evaluator
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult


def test_immutable_evaluator_contract():
    result = ProcessResult("p", "a", State.FAILED, Outcome(OutcomeStatus.PARTIAL, "partial"), usage={"tokens": 1})
    request = EvaluationInput("group", (("candidate", result.to_json()),), '{"goal":"quality"}')
    request.results()[0][1].usage["tokens"] = 999
    assert request.results()[0][1].usage["tokens"] == 1
    evaluation = Evaluation("judge", "ok", (("candidate", 0.5),), rationale="incomplete")
    assert asyncio.run(run_evaluator(FakeEvaluator(evaluation), request)) == evaluation
    invalid = Evaluation("judge", "ok", (("unknown", 1.0),))
    assert asyncio.run(run_evaluator(FakeEvaluator(invalid), request)).status == "error"
    unavailable = Evaluation("judge", "unavailable", rationale="offline")
    assert asyncio.run(run_evaluator(FakeEvaluator(unavailable), request)) == unavailable
    with pytest.raises(ValueError):
        Evaluation("judge", "ok", (("candidate", float("nan")),))
