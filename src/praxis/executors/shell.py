"""Explicit POSIX shell execution, gated by kernel-issued shell authority."""

from dataclasses import replace

from praxis.executors.features import ExecutorFeatures
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.workspaces.local import LocalWorkspaces


class ShellExecutor(LocalProcessExecutor):
    def __init__(self, workspaces: LocalWorkspaces, authority: Authority):
        super().__init__(workspaces)
        self.authority = authority

    @property
    def descriptor(self) -> ExecutorFeatures:
        return replace(super().descriptor, name="shell")

    async def start(self, request: ExecutionRequest) -> ControlResult:
        decision = self.authority.authorize(request.process_id, Resource.EXECUTOR, "shell", "shell")
        if not decision.allowed:
            return ControlResult(True, False, "shell_capability_denied")
        command = request.spec.inputs.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            return ControlResult(True, False, "invalid_shell_command")
        if "argv" in request.spec.inputs:
            return ControlResult(True, False, "ambiguous_shell_input")
        inputs = {key: value for key, value in request.spec.inputs.items() if key != "command"}
        inputs["argv"] = ["/bin/sh", "-c", command]
        return await super().start(replace(request, spec=replace(request.spec, inputs=inputs)))
