import asyncio

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.lifecycle import State
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_allow_deny_constraints_and_audit():
    authority = Authority()
    assert not authority.authorize("p", Resource.EXECUTOR, "execute", "local").allowed
    authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "local")
    assert authority.authorize("p", Resource.EXECUTOR, "execute", "local").allowed
    assert not authority.authorize("other", Resource.EXECUTOR, "execute", "local").allowed
    assert not authority.authorize("p", Resource.EXECUTOR, "shell", "local").allowed
    assert len([e for e in authority.events if e.type == "capability.decision"]) == 4


def test_kernel_denies_without_grant(tmp_path):
    async def exercise():
        fake = FakeExecutor()
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"fake": fake})
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.spawn(parent.process_id, ProcessSpec("child", "fake"))
        result = (await kernel.join(parent.process_id, [child]))[0]
        assert result.state == State.FAILED
        assert result.outcome.reason == "capability_denied"
        assert not fake.requests
    asyncio.run(exercise())
