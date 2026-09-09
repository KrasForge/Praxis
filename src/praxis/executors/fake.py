"""Deterministic executor for kernel integration and conformance tests."""

from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import (
    Checkpoint, CheckpointResult, ControlResult, ExecutionRequest,
)


class FakeExecutor:
    def __init__(self, outcome: Outcome | None = None):
        self.outcome = outcome or Outcome(OutcomeStatus.COMPLETED, "fake.completed")
        self.requests: dict[str, ExecutionRequest] = {}
        self.results: dict[str, Outcome] = {}

    @property
    def descriptor(self) -> ExecutorFeatures:
        return ExecutorFeatures("fake", frozenset({"cancel"}))

    async def start(self, request: ExecutionRequest) -> ControlResult:
        if request.attempt_id in self.requests:
            same = self.requests[request.attempt_id] == request
            return ControlResult(True, same, "duplicate" if same else "identity_conflict")
        self.requests[request.attempt_id] = request
        self.results[request.attempt_id] = self.outcome
        return ControlResult(True, True, "started")

    async def signal(self, attempt_id: str, signal: str) -> ControlResult:
        return ControlResult(False, False, "signal_unavailable")

    async def checkpoint(self, attempt_id: str) -> CheckpointResult:
        return CheckpointResult(ControlResult(False, False, "checkpoint_unavailable"))

    async def restore(self, request: ExecutionRequest, checkpoint: Checkpoint) -> ControlResult:
        return ControlResult(False, False, "restore_unavailable")

    async def cancel(self, attempt_id: str) -> ControlResult:
        if attempt_id not in self.requests:
            return ControlResult(True, False, "attempt_not_found")
        return ControlResult(True, False, "already_terminal")

    async def collect_result(self, attempt_id: str) -> Outcome:
        return self.results.get(attempt_id, Outcome(OutcomeStatus.UNAVAILABLE, "attempt_not_found"))
