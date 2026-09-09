import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.authority import Authority
from praxis.kernel.joins import ChildFailurePolicy, JoinPolicy, join_children
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("policy,quorum,expected", [(JoinPolicy.ALL, None, "failed"),
                                                    (JoinPolicy.ANY, None, "completed"),
                                                    (JoinPolicy.QUORUM, 2, "failed")])
def test_mixed_child_join_policies(tmp_path, policy, quorum, expected):
    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"), {
            "ok": FakeExecutor(), "fail": FakeExecutor(Outcome(OutcomeStatus.FAILED, "failure")),
        }, authority=Authority(execution_defaults=frozenset({"ok", "fail"})))
        parent = kernel.create(ProcessSpec("parent", "ok"))
        ok = kernel.spawn(parent.process_id, ProcessSpec("ok", "ok"))
        failed = kernel.spawn(parent.process_id, ProcessSpec("fail", "fail"))
        cancelled = kernel.create(ProcessSpec("cancel", "ok"), parent.process_id)
        await kernel.cancel(cancelled.process_id)
        await kernel.join(parent.process_id, [ok, failed])
        children = (ok, failed, cancelled.process_id)
        report = await join_children(kernel, parent.process_id, children, policy=policy, quorum=quorum)
        assert report.status == expected
        assert len(report.outcomes) == 3
        assert (await join_children(kernel, parent.process_id, children,
                                    failure_policy=ChildFailurePolicy.ESCALATE)).status == "escalated"
        assert (await join_children(kernel, parent.process_id, children,
                                    failure_policy=ChildFailurePolicy.FAIL_FAST)).status == "failed"
    asyncio.run(exercise())


def test_join_retries_transient_child(tmp_path):
    class Flaky(FakeExecutor):
        calls = 0

        async def start(self, request):
            self.calls += 1
            self.outcome = (Outcome(OutcomeStatus.FAILED, "transient", retryable=True)
                            if self.calls == 1 else Outcome(OutcomeStatus.COMPLETED, "done"))
            return await super().start(request)

    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"fake": Flaky()}, authority=Authority(execution_defaults=frozenset({"fake"})))
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.spawn(parent.process_id, ProcessSpec("child", "fake"))
        report = await join_children(kernel, parent.process_id, (child,), failure_policy=ChildFailurePolicy.RETRY)
        assert report.status == "completed"
        assert len({entry.attempt_id for entry in kernel.processes[child].history}) == 2
    asyncio.run(exercise())
