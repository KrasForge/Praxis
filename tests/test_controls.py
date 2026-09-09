import asyncio
import sys

from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.lifecycle import State
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import CancellationPolicy, Kernel, ProcessSignal
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_suspend_resume_and_tree_cancel(tmp_path):
    async def exercise():
        workspaces = LocalWorkspaces(tmp_path / "ws")
        kernel = Kernel(ProcessRecords(tmp_path / "records"), workspaces,
                        {"local": LocalProcessExecutor(workspaces)},
                        authority=Authority(execution_defaults=frozenset({"local"})))
        parent = kernel.create(ProcessSpec("parent", "local"))
        child = kernel.spawn(parent.process_id, ProcessSpec("wait", "local", inputs={
            "argv": [sys.executable, "-c", "import time; time.sleep(60)"], "timeout": 10,
        }))
        assert (await kernel.signal(child, ProcessSignal.SUSPEND)).applied
        assert kernel.processes[child].state == State.SUSPENDED
        assert not (await kernel.signal(child, ProcessSignal.SUSPEND)).applied
        assert (await kernel.signal(child, ProcessSignal.RESUME)).applied
        assert kernel.processes[child].state == State.RUNNING
        results = await kernel.cancel(parent.process_id, CancellationPolicy.TREE)
        assert all(result.applied for result in results.values())
        assert kernel.processes[child].state == State.CANCELLED
        assert parent.state == State.CANCELLED
        assert any(event.type == "process.control" for event in kernel.events)
        assert not (await kernel.cancel(child))[child].applied
    asyncio.run(exercise())


def test_self_cancellation_preserves_children(tmp_path):
    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"), {},
                        authority=Authority(execution_defaults=frozenset({"none"})))
        parent = kernel.create(ProcessSpec("parent", "none"))
        child = kernel.create(ProcessSpec("child", "none"), parent.process_id)
        await kernel.cancel(parent.process_id)
        assert child.state == State.PENDING
    asyncio.run(exercise())
