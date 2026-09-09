import asyncio

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.lifecycle import State
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.supervisor import DelegationRequest, Supervisor
from praxis.workspaces.local import LocalWorkspaces


def test_nested_supervisor_forks_isolated_children(tmp_path):
    async def exercise():
        authority = Authority(execution_defaults=frozenset({"fake"}))
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"fake": FakeExecutor()}, authority=authority, retain_workspaces=True)
        root = kernel.create(ProcessSpec("root", "fake"))
        nested = kernel.create(ProcessSpec("nested", "fake"), root.process_id)
        grant = authority.issue(nested.process_id, Resource.FILESYSTEM, frozenset({"read"}), str(tmp_path))
        supervisor = Supervisor(kernel, nested.process_id)
        children = supervisor.fork((ProcessSpec("a", "fake"), ProcessSpec("b", "fake")), delegations=(
            (DelegationRequest(grant.capability_id, grant.actions, str(tmp_path)),), (),
        ))
        await kernel.join(nested.process_id, list(children))
        assert all(status.state == State.COMPLETED for status in supervisor.monitor())
        assert len({kernel.handles[c].workspace_id for c in children}) == 2
        assert authority.authorize(children[0], Resource.FILESYSTEM, "read", str(tmp_path)).allowed
        assert not authority.authorize(children[1], Resource.FILESYSTEM, "read", str(tmp_path)).allowed
        assert Supervisor(kernel, root.process_id).monitor()[0].process_id == nested.process_id
    asyncio.run(exercise())
