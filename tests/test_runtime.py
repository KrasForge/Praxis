import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.lifecycle import State
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_nested_spawn_join_partial_failure(tmp_path):
    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"), {
            "ok": FakeExecutor(),
            "fail": FakeExecutor(Outcome(OutcomeStatus.FAILED, "fixture.failed")),
        })
        parent = kernel.create(ProcessSpec("supervise", "ok"))
        child = kernel.create(ProcessSpec("nested", "ok"), parent.process_id)
        nested = kernel.spawn(child.process_id, ProcessSpec("worker", "ok"))
        assert (await kernel.join(child.process_id, [nested]))[0].state == State.COMPLETED
        kernel.start(child.process_id)
        failed = kernel.spawn(parent.process_id, ProcessSpec("failure", "fail"))
        outcomes = await kernel.join(parent.process_id, [child.process_id, failed])
        assert [o.state for o in outcomes] == [State.COMPLETED, State.FAILED]
        assert kernel.records.load(nested).parent_id == child.process_id
        assert kernel.events[-1].type == "process.state"
        with pytest.raises(ValueError):
            await kernel.join(parent.process_id, [nested])
    asyncio.run(exercise())


def test_unavailable_and_unverified_fail_closed(tmp_path):
    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"ok": FakeExecutor()})
        parent = kernel.create(ProcessSpec("parent", "ok"))
        missing = kernel.spawn(parent.process_id, ProcessSpec("work", "missing"))
        unverified = kernel.spawn(parent.process_id, ProcessSpec("work", "ok", contract={"check": 1}))
        outcomes = await kernel.join(parent.process_id, [missing, unverified])
        assert all(o.state == State.FAILED for o in outcomes)
        assert outcomes[0].outcome.status == OutcomeStatus.UNAVAILABLE
    asyncio.run(exercise())
