import asyncio
import sys

import pytest

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.contracts import Check
from praxis.validators.command import CommandValidator
from praxis.validators.protocol import CheckStatus, ValidationInput


@pytest.mark.parametrize("program,status", [
    ("print('passed')", CheckStatus.PASS),
    ("import sys; print('failed',file=sys.stderr); sys.exit(1)", CheckStatus.FAIL),
    ("import time; time.sleep(60)", CheckStatus.ERROR),
])
def test_command_results_and_timeout(program, status):
    authority = Authority()
    authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "validator.command")
    validator = CommandValidator(authority)
    source = ValidationInput("p", "a", "s", (("input", b"original"),))
    result = asyncio.run(validator.validate(source, Check("c", "command", parameters={
        "argv": [sys.executable, "-c", program], "timeout": 0.2,
    })))
    assert result.status == status
    if status == CheckStatus.ERROR:
        assert result.reason == "deadline_exceeded"
    assert source.files == (("input", b"original"),)


def test_denied_missing_program_and_copy_isolation():
    authority = Authority()
    validator = CommandValidator(authority)
    source = ValidationInput("p", "a", "s", (("input", b"original"),))
    check = Check("c", "command", parameters={"argv": ["/nonexistent-praxis-validator"]})
    assert asyncio.run(validator.validate(source, check)).status == CheckStatus.UNAVAILABLE
    authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "validator.command")
    assert asyncio.run(validator.validate(source, check)).status == CheckStatus.ERROR
    mutation = Check("c", "command", parameters={"argv": [sys.executable, "-c",
                      "from pathlib import Path; Path('input').write_text('changed')"]})
    assert asyncio.run(validator.validate(source, mutation)).status == CheckStatus.PASS
    assert source.files[0][1] == b"original"
