import subprocess
import sys

import pytest


@pytest.mark.parametrize("args,code,text", [
    (["--help"], 0, "Praxis execution runtime"),
    (["--version"], 0, "praxis 0.1.0"),
    (["unknown"], 2, "unrecognized arguments"),
])
def test_cli(args, code, text):
    result = subprocess.run(
        [sys.executable, "-m", "praxis", *args], capture_output=True, text=True
    )
    assert result.returncode == code
    assert text in result.stdout + result.stderr
