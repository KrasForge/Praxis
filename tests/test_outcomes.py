import pytest

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.lifecycle import State


@pytest.mark.parametrize("status", list(OutcomeStatus))
@pytest.mark.parametrize("verified", [True, False])
def test_success_requires_completed_and_verified(status, verified):
    outcome = Outcome(status, "test.result")
    assert (outcome.process_state(verified=verified) == State.COMPLETED) == (
        status == OutcomeStatus.COMPLETED and verified
    )
    assert Outcome.from_json(outcome.to_json()) == outcome


def test_nonzero_completion_rejected():
    with pytest.raises(ValueError):
        Outcome(OutcomeStatus.COMPLETED, "exit", exit_code=1)
