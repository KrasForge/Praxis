"""Command validation in a disposable copy of immutable input bytes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.contracts import Check
from praxis.kernel.spec import ProcessSpec
from praxis.validators.protocol import CheckResult, CheckStatus, ValidationInput
from praxis.workspaces.local import LocalWorkspaces


class CommandValidator:
    protocol_version = 1

    def __init__(self, authority: Authority):
        self.authority = authority

    async def validate(self, source: ValidationInput, check: Check) -> CheckResult:
        decision = self.authority.authorize(source.process_id, Resource.EXECUTOR,
                                            "execute", "validator.command")
        if not decision.allowed:
            return CheckResult(check.check_id, CheckStatus.UNAVAILABLE, "capability_denied")
        argv = check.parameters.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            return CheckResult(check.check_id, CheckStatus.ERROR, "invalid_validator_argv")
        with TemporaryDirectory(prefix="praxis-validator-") as directory:
            provider = LocalWorkspaces(Path(directory))
            handle = provider.create(source.process_id)
            path = provider.path_for(handle, source.process_id)
            for relative, content in source.files:
                target = path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            spec = ProcessSpec("validate", "local", inputs={
                "argv": argv, "timeout": check.parameters.get("timeout", 30),
                "stdin": check.parameters.get("stdin", ""),
            })
            executor = LocalProcessExecutor(provider)
            request = ExecutionRequest(source.process_id, str(uuid4()), spec, handle.workspace_id, path)
            started = await executor.start(request)
            if not started.applied:
                return CheckResult(check.check_id, CheckStatus.ERROR, started.reason)
            outcome = await executor.collect_result(request.attempt_id)
            status = {OutcomeStatus.COMPLETED: CheckStatus.PASS,
                      OutcomeStatus.FAILED: CheckStatus.FAIL}.get(outcome.status, CheckStatus.ERROR)
            return CheckResult(check.check_id, status, outcome.reason, outcome.stdout,
                               outcome.stderr, outcome.exit_code)
