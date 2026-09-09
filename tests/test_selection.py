import pytest

from praxis.evaluators.protocol import Evaluation
from praxis.evaluators.selection import SelectionPolicy


@pytest.mark.parametrize("mode", ["score", "pairwise", "quorum", "human"])
def test_selection_modes(mode):
    policy = SelectionPolicy(mode, frozenset({"a", "b"}), frozenset({"judge"}))
    evaluation = Evaluation("judge", "ok", (("a", 2.0), ("b", 1.0)), (("a", "b", 1),))
    selected = policy.select((evaluation,), **({"human_choice": "a", "actor": "operator"} if mode == "human" else {}))
    assert selected.status == "selected" and selected.winners == ("a",)
    assert selected.evaluations == (evaluation,)


def test_ties_errors_and_empty_eligibility():
    evaluation = Evaluation("judge", "ok", (("a", 1.0), ("b", 1.0)))
    policy = SelectionPolicy("score", frozenset({"a", "b"}), frozenset({"judge"}))
    assert policy.select((evaluation,)).status == "tie"
    assert policy.select((Evaluation("judge", "error"),)).status == "unresolved"
    assert SelectionPolicy("score", frozenset(), frozenset()).select(()).status == "no_valid_candidate"
    with pytest.raises(ValueError):
        policy.select((evaluation, evaluation))
    quorum = SelectionPolicy("quorum", policy.eligible_candidates, policy.eligible_evaluators, quorum=2)
    assert quorum.select((evaluation,)).status == "unresolved"
