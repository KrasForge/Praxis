import asyncio
from dataclasses import FrozenInstanceError

import pytest

from praxis.kernel.contracts import Check
from praxis.validators.protocol import CheckResult, CheckStatus, FakeValidator, ValidationInput, Validator


@pytest.mark.parametrize("status", list(CheckStatus))
def test_validator_conformance_and_roundtrip(status):
    source = ValidationInput("p", "a", "snapshot", (("file", b"original"),))
    validator: Validator = FakeValidator(status)
    assert isinstance(validator, Validator)
    result = asyncio.run(validator.validate(source, Check("c", "fake")))
    assert result.status == status
    assert CheckResult.from_json(result.to_json()) == result
    assert source.files == (("file", b"original"),)
    with pytest.raises(FrozenInstanceError):
        source.files = ()


def test_mutable_or_escaped_inputs_rejected():
    with pytest.raises(ValueError):
        ValidationInput("p", "a", "s", [("file", b"data")])
    with pytest.raises(ValueError):
        ValidationInput("p", "a", "s", (("../host", b"data"),))
