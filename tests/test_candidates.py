from dataclasses import replace

import pytest

from praxis.kernel.candidates import Candidate, CandidateGroup, CandidateState


def test_group_roundtrip_and_transitions():
    group = CandidateGroup("parent", "common task", (Candidate("a", "p1"), Candidate("b", "p2")))
    assert CandidateGroup.from_json(group.to_json()) == group
    with pytest.raises(ValueError):
        group.move(CandidateState.SELECTED, "a")
    group = group.move(CandidateState.RUNNING).move(CandidateState.EVALUATING)
    with pytest.raises(ValueError):
        group.move(CandidateState.SELECTED, "unknown")
    selected = group.move(CandidateState.SELECTED, "a")
    assert CandidateGroup.from_json(selected.to_json()) == selected
    with pytest.raises(ValueError):
        selected.move(CandidateState.RUNNING)
    with pytest.raises(ValueError):
        replace(group, candidates=(Candidate("a", "p1"), Candidate("a", "p2")))
