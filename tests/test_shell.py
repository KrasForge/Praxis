import asyncio

import pytest

from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.executors.shell import ShellExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


@pytest.mark.parametrize("mode", ["success", "cancel", "timeout", "signal"])
def test_shell_gate_and_controls(tmp_path, mode):
    async def exercise():
        provider = LocalWorkspaces(tmp_path)
        authority = Authority()
        handle = provider.create("p")
        command = "printf '%s' shell-output" if mode == "success" else "sleep 60"
        spec = ProcessSpec("shell", "shell", inputs={"command": command, "timeout": 0.1 if mode == "timeout" else 10})
        request = ExecutionRequest("p", "a", spec, handle.workspace_id, provider.path_for(handle, "p"))
        executor = ShellExecutor(provider, authority)
        assert not (await executor.start(request)).applied
        assert not executor.processes
        authority.issue("p", Resource.EXECUTOR, frozenset({"shell"}), "shell")
        assert (await executor.start(request)).applied
        if mode == "cancel":
            assert (await executor.cancel("a")).applied
        if mode == "signal":
            assert (await executor.signal("a", "terminate")).applied
        outcome = await executor.collect_result("a")
        assert outcome.status == {"success": OutcomeStatus.COMPLETED, "cancel": OutcomeStatus.CANCELLED,
                                  "timeout": OutcomeStatus.TIMED_OUT, "signal": OutcomeStatus.FAILED}[mode]
        if mode == "success":
            assert outcome.stdout == "shell-output"
    asyncio.run(exercise())
