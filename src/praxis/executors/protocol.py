"""Version-one asynchronous harness boundary."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome
from praxis.kernel.spec import ProcessSpec


@dataclass(frozen=True)
class ExecutionRequest:
    process_id: str
    attempt_id: str
    spec: ProcessSpec
    workspace_id: str
    workspace_path: Path
    protocol_version: int = 1
    parent_id: str | None = None
    lineage_json: str | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != 1 or type(self.protocol_version) is not int:
            raise ValueError("unsupported executor protocol")
        if not all((self.process_id, self.attempt_id, self.workspace_id)):
            raise ValueError("execution identity required")


@dataclass(frozen=True)
class ControlResult:
    supported: bool
    applied: bool
    reason: str

    def __post_init__(self) -> None:
        if self.applied and not self.supported:
            raise ValueError("unsupported control cannot be applied")


@dataclass(frozen=True)
class Checkpoint:
    executor: str
    process_id: str
    attempt_id: str
    payload: bytes
    protocol_version: int = 1


@dataclass(frozen=True)
class CheckpointResult:
    control: ControlResult
    checkpoint: Checkpoint | None = None


@runtime_checkable
class Executor(Protocol):
    @property
    def descriptor(self) -> ExecutorFeatures: ...

    async def start(self, request: ExecutionRequest) -> ControlResult: ...
    async def signal(self, attempt_id: str, signal: str) -> ControlResult: ...
    async def checkpoint(self, attempt_id: str) -> CheckpointResult: ...
    async def restore(self, request: ExecutionRequest, checkpoint: Checkpoint) -> ControlResult: ...
    async def cancel(self, attempt_id: str) -> ControlResult: ...
    async def collect_result(self, attempt_id: str) -> Outcome: ...


@runtime_checkable
class CommitGuard(Protocol):
    def commit_allowed(self, attempt_id: str) -> bool: ...
