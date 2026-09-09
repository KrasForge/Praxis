import pytest

from praxis.kernel.contracts import Check, Contract
from praxis.validators.policy import evaluate
from praxis.validators.protocol import CheckResult, CheckStatus, ValidationInput


SOURCE = ValidationInput("p", "a", "snapshot", (("output", b"done"),))


@pytest.mark.parametrize("status", list(CheckStatus))
def test_required_and_advisory(status):
    contract = Contract(required_outputs=("output",), validators=(Check("required", "fake"),
                        Check("advice", "fake", required=False)))
    report = evaluate(contract, SOURCE, (CheckResult("required", status, "test"),
                                        CheckResult("advice", CheckStatus.FAIL, "test")))
    assert report.approved == (status == CheckStatus.PASS)
    assert report.advisory_failures == ("advice",)
    assert not evaluate(contract, SOURCE, ()).approved


def test_quorum_missing_outputs_and_duplicates():
    contract = Contract(validators=tuple(Check(k, "fake", False) for k in ("a", "b", "c")),
                        quorum=2, quorum_checks=("a", "b", "c"))
    assert Contract.from_json(contract.to_json()) == contract
    passing = (CheckResult("a", CheckStatus.PASS, "test"), CheckResult("b", CheckStatus.PASS, "test"))
    assert evaluate(contract, SOURCE, passing).approved
    assert not evaluate(contract, SOURCE, passing[:1]).approved
    assert not evaluate(Contract(required_outputs=("missing",)), SOURCE, ()).approved
    with pytest.raises(ValueError):
        evaluate(contract, SOURCE, passing + passing)
