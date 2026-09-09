import pytest

from praxis.kernel.lifecycle import State, TransitionError, transition


LEGAL = {
    ("pending", "running"), ("pending", "failed"), ("pending", "cancelled"),
    ("running", "suspended"), ("running", "completed"), ("running", "failed"),
    ("running", "cancelled"), ("suspended", "running"),
    ("suspended", "failed"), ("suspended", "cancelled"),
}


@pytest.mark.parametrize("source", list(State))
@pytest.mark.parametrize("target", list(State))
def test_every_transition(source, target):
    if (source.value, target.value) in LEGAL:
        assert transition(source, target) == target
    else:
        with pytest.raises(TransitionError) as exc:
            transition(source, target)
        assert exc.value.code == "invalid_transition"
