import asyncio
import json
import sys

import pytest

from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.contracts import Check, Contract
from praxis.kernel.lifecycle import State
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.validators.protocol import CheckStatus, FakeValidator
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory


@pytest.mark.parametrize("status,required,expected", [
    (CheckStatus.PASS, True, State.COMPLETED),
    (CheckStatus.FAIL, True, State.FAILED),
    (CheckStatus.UNAVAILABLE, True, State.FAILED),
    (CheckStatus.FAIL, False, State.COMPLETED),
])
def test_exit_verify_commit_or_rollback(tmp_path, status, required, expected):
    async def exercise():
        canonical = CanonicalDirectory(tmp_path / "canonical")
        (canonical.path / "output").write_text("old")
        workspaces = LocalWorkspaces(tmp_path / "ws")
        authority = Authority(execution_defaults=frozenset({"local"}))
        kernel = Kernel(ProcessRecords(tmp_path / "records"), workspaces,
                        {"local": LocalProcessExecutor(workspaces)}, authority=authority,
                        validators={"fake": FakeValidator(status)}, retain_workspaces=True)
        contract = Contract(required_outputs=("output",), validators=(Check("check", "fake", required),))
        process = kernel.create(ProcessSpec("update", "local", inputs={"argv": [sys.executable, "-c",
                                "from pathlib import Path; Path('output').write_text('new')"]},
                                contract=json.loads(contract.to_json())), canonical=canonical)
        authority.issue(process.process_id, Resource.WORKSPACE, frozenset({"commit"}), process.process_id)
        authority.issue(process.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        assert process.state == expected
        assert (canonical.path / "output").read_text() == ("new" if expected == State.COMPLETED else "old")
        report = kernel.verification[process.process_id]
        assert report.approved == (expected == State.COMPLETED)
        if not required:
            assert report.advisory_failures == ("check",)
        events = [e.type for e in kernel.events]
        assert "contract.checked" in events
        assert ("workspace.committed" in events) == (expected == State.COMPLETED)
    asyncio.run(exercise())
