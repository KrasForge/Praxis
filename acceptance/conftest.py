"""Live acceptance against real providers: opt-in, never on pull requests.

Every check skips unless its credentials and switches are present in the environment
(see docs/acceptance.md). A run writes a dated record to $PRAXIS_ACCEPTANCE_RECORD
(default: acceptance-record.json) with the result of every check and the provider
versions it saw. Before the record is written, it and every runtime database and
workspace the checks created are scanned for the secret values in SECRET_VARIABLES;
a leak fails the run.
"""

import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from praxis import __version__

# Environment variables whose values must never appear in a record, journal or workspace.
SECRET_VARIABLES = (
    "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "PRAXIS_ACCEPTANCE_NOESIS_TOKEN", "PRAXIS_ACCEPTANCE_HOST_TOKEN",
)

RECORD: dict[str, object] = {
    "praxis": __version__,
    "python": platform.python_version(),
    "platform": sys.platform,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "versions": {},
    "checks": [],
}
SCANNED: list[Path] = []


def secrets() -> list[str]:
    return [os.environ[name] for name in SECRET_VARIABLES if len(os.environ.get(name, "")) >= 8]


def leaks(paths: list[Path], values: list[str]) -> list[str]:
    """Paths (and the record) whose bytes contain a secret value."""
    found = []
    needles = [value.encode() for value in values]
    for root in paths:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
        for file in files:
            try:
                data = file.read_bytes()
            except OSError:
                continue
            if any(needle in data for needle in needles):
                found.append(str(file))
    return found


@pytest.fixture
def scanned(tmp_path: Path) -> Path:
    """A data directory whose contents are checked for leaked secrets at the end of the run."""
    SCANNED.append(tmp_path)
    return tmp_path


@pytest.fixture
def versions() -> dict[str, str]:
    table = RECORD["versions"]
    assert isinstance(table, dict)
    return table


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" or (report.when == "setup" and report.skipped):
        detail = ""
        if report.skipped and isinstance(report.longrepr, tuple):
            detail = str(report.longrepr[2]).removeprefix("Skipped: ")
        elif report.failed:
            detail = str(report.longrepr).splitlines()[-1] if report.longrepr else "failed"
        checks = RECORD["checks"]
        assert isinstance(checks, list)
        checks.append({"check": item.nodeid.split("::", 1)[-1], "outcome": report.outcome, "detail": detail})


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    RECORD["finished_at"] = datetime.now(timezone.utc).isoformat()
    values = secrets()
    raw = json.dumps(RECORD, indent=2, sort_keys=True)
    leaked = leaks(SCANNED, values) + (["record"] if any(v in raw for v in values) else [])
    RECORD["secret_canary"] = {"values_checked": len(values), "leaks": leaked}
    raw = json.dumps(RECORD, indent=2, sort_keys=True)
    if any(v in raw for v in values):  # a leaked value can appear in a path name only in theory
        raw = json.dumps({**RECORD, "checks": "withheld: record contained a secret value"}, indent=2)
    Path(os.environ.get("PRAXIS_ACCEPTANCE_RECORD", "acceptance-record.json")).write_text(raw + "\n")
    if leaked:
        print(f"\nsecret canary: {len(leaked)} location(s) contained a secret value", file=sys.stderr)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
