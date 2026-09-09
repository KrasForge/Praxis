"""Codex CLI integration; native options never escape the codex namespace.

Interface: https://learn.chatgpt.com/docs/non-interactive-mode
The host must provision Codex credentials in the explicit process environment.
"""

import json
from dataclasses import replace

from praxis.executors.features import ExecutorFeatures
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.workspaces.local import LocalWorkspaces


class CodexExecutor(LocalProcessExecutor):
    def __init__(self, workspaces: LocalWorkspaces, authority: Authority,
                 executable: str = "codex"):
        super().__init__(workspaces)
        self.authority = authority
        self.executable = executable

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("codex", frozenset({"cancel"}))

    def map_request(self, request: ExecutionRequest) -> ExecutionRequest:
        options = request.spec.metadata.get("codex", {})
        if not isinstance(options, dict) or set(options) - {"model"}:
            raise ValueError("invalid_codex_options")
        model = options.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()
                                  or "\x00" in model):
            raise ValueError("invalid_codex_model")
        writable = self.authority.authorize(
            request.process_id, Resource.FILESYSTEM, "write", str(request.workspace_path)
        ).allowed
        argv = [self.executable, "exec", "--json", "--cd", str(request.workspace_path),
                "--sandbox", "workspace-write" if writable else "read-only",
                "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "-c", "approval_policy=\"never\"",
                "-c", "sandbox_workspace_write.network_access=false"]
        if model is not None:
            argv.extend(["--model", model])
        argv.append("-")
        prompt = json.dumps({"objective": request.spec.objective, "inputs": request.spec.inputs},
                            ensure_ascii=False, allow_nan=False)
        return replace(request, spec=replace(request.spec, inputs={"argv": argv, "stdin": prompt}))

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if not self.authority.authorize(
            request.process_id, Resource.EXECUTOR, "execute", "codex"
        ).allowed:
            return ControlResult(True, False, "codex_capability_denied")
        try:
            mapped = self.map_request(request)
        except ValueError:
            return ControlResult(True, False, "invalid_codex_options")
        return await super().start(mapped)
