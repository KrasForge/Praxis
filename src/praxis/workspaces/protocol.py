"""Opaque workspace identities and provider conformance boundary."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class WorkspaceError(ValueError):
    code = "workspace_error"


class UnsupportedWorkspaceOperation(WorkspaceError):
    code = "workspace_operation_unavailable"


@dataclass(frozen=True)
class WorkspaceHandle:
    workspace_id: str
    process_id: str
    provider: str

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v for v in (
            self.workspace_id, self.process_id, self.provider
        )):
            raise WorkspaceError("workspace identity required")


@dataclass(frozen=True)
class WorkspaceInfo:
    handle: WorkspaceHandle
    retained: bool
    features: frozenset[str]


@dataclass(frozen=True)
class Snapshot:
    workspace_id: str
    snapshot_id: str
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class WorkspaceDiff:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


@runtime_checkable
class WorkspaceProvider(Protocol):
    protocol_version: int

    def create(self, process_id: str, *, retain: bool = False) -> WorkspaceHandle: ...
    def inspect(self, handle: WorkspaceHandle) -> WorkspaceInfo: ...
    def snapshot(self, handle: WorkspaceHandle) -> Snapshot: ...
    def diff(self, handle: WorkspaceHandle, baseline: Snapshot) -> WorkspaceDiff: ...
    def destroy(self, handle: WorkspaceHandle) -> None: ...
