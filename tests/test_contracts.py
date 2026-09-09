import json

import pytest

from praxis.kernel.contracts import Check, Contract
from praxis.kernel.spec import ProcessSpec, SpecError


def test_contract_in_spec_roundtrip():
    contract = Contract(("output.txt",), (Check("invariant", "fake"),),
                        (Check("advice", "fake", required=False),), ("invariant",))
    spec = ProcessSpec("work", "local", contract=json.loads(contract.to_json()))
    restored = ProcessSpec.from_json(spec.to_json())
    assert Contract.from_json(json.dumps(restored.contract)) == contract
    assert not contract.validators[0].required


@pytest.mark.parametrize("data", [
    {"required_outputs": ["../outside"]}, {"required_outputs": ["/host"]},
    {"validators": [{"check_id": "x", "validator": "fake", "required": "yes"}]},
    {"acceptance_checks": ["missing"]}, {"extra": True},
])
def test_invalid_contracts_rejected_by_spec(data):
    with pytest.raises(SpecError) as exc:
        ProcessSpec("work", "local", contract=data)
    assert exc.value.field == "contract"
