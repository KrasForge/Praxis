"""Release-boundary regressions complement the full integration matrix."""
import asyncio
import base64
import sys

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.remote.bundle import WorkspaceBundle
from praxis.storage.memory import MemoryStore
from praxis.workspaces.local import LocalWorkspaces


def test_budget_rejects_before_start(tmp_path):
    class NoCancel(FakeExecutor):
        @property
        def descriptor(self):
            return ExecutorFeatures("fake")
    async def exercise():
        for index, (executor, limit, reason) in enumerate([
            (FakeExecutor(), 0, "wall_budget_exhausted"),
            (NoCancel(), 10000, "budget_cancellation_unavailable"),
        ]):
            kernel = Kernel(MemoryStore(), LocalWorkspaces(tmp_path / str(index)), {"fake": executor},
                            authority=Authority(execution_defaults=frozenset({"fake"})))
            process = kernel.create(ProcessSpec("do not launch", "fake", budget={"wall_milliseconds": limit}))
            kernel.start(process.process_id)
            outcome = await kernel.tasks[process.process_id]
            assert outcome.reason == reason
            assert not executor.requests
    asyncio.run(exercise())


def test_output_flood_is_bounded_and_terminated(tmp_path):
    async def exercise():
        provider = LocalWorkspaces(tmp_path)
        handle = provider.create("p")
        executor = LocalProcessExecutor(provider, max_output_bytes=4096)
        request = ExecutionRequest("p", "a", ProcessSpec("flood", "local", inputs={"argv": [sys.executable, "-c",
            "import os\nwhile True: os.write(1, b'x'*8192)"]}), handle.workspace_id, provider.path_for(handle, "p"))
        assert (await executor.start(request)).applied
        result = await asyncio.wait_for(executor.collect_result("a"), 5)
        assert result.reason == "output_limit_exceeded" and len(result.stdout) <= 4096
        assert executor.processes["a"].returncode is not None
    asyncio.run(exercise())


def test_bundle_restores_readonly_directory_after_children(tmp_path):
    provider = LocalWorkspaces(tmp_path)
    handle = provider.create("p")
    bundle = WorkspaceBundle((("directory", None, 0o555), ("directory/file", base64.b64encode(b"data").decode(), 0o444)))
    bundle.restore(provider, handle)
    path = provider.path_for(handle, "p")
    assert (path / "directory/file").read_bytes() == b"data"
    assert (path / "directory").stat().st_mode & 0o777 == 0o555
    (path / "directory").chmod(0o755)
