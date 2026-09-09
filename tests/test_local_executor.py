import asyncio
import sys

import pytest

from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("code,status", [(0, OutcomeStatus.COMPLETED), (7, OutcomeStatus.FAILED)])
def test_local_io_environment_cwd_and_exit(tmp_path, code, status):
    async def exercise():
        provider = LocalWorkspaces(tmp_path)
        handle = provider.create("p")
        path = provider.path_for(handle, "p")
        spec = ProcessSpec("run", "local", inputs={
            "argv": [sys.executable, "-c", "import os,sys; print(os.getcwd()); "
                     "print(os.environ.get('ONLY')); print(sys.stdin.read()); "
                     f"print('error',file=sys.stderr); sys.exit({code})"],
            "stdin": "literal $(touch escaped)",
        }, environment={"ONLY": "explicit"})
        executor = LocalProcessExecutor(provider)
        request = ExecutionRequest("p", "a", spec, handle.workspace_id, path)
        assert (await executor.start(request)).applied
        result = await executor.collect_result("a")
        assert result.status == status
        assert str(path) in result.stdout and "explicit" in result.stdout
        assert "$(touch escaped)" in result.stdout
        assert result.stderr == "error\n"
        assert not (path / "escaped").exists()
    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel(tmp_path, cancel):
    async def exercise():
        provider = LocalWorkspaces(tmp_path)
        handle = provider.create("p")
        spec = ProcessSpec("wait", "local", inputs={
            "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
            "timeout": 0.1 if not cancel else 10,
        })
        executor = LocalProcessExecutor(provider)
        request = ExecutionRequest("p", "a", spec, handle.workspace_id,
                                   provider.path_for(handle, "p"))
        assert (await executor.start(request)).applied
        if cancel:
            assert (await executor.cancel("a")).applied
        result = await executor.collect_result("a")
        assert result.status == (OutcomeStatus.CANCELLED if cancel else OutcomeStatus.TIMED_OUT)
    asyncio.run(exercise())
