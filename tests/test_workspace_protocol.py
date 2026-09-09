import pytest

from praxis.workspaces.protocol import (
    Snapshot, UnsupportedWorkspaceOperation, WorkspaceDiff, WorkspaceHandle,
    WorkspaceInfo, WorkspaceProvider,
)


class MinimalProvider:
    protocol_version = 1

    def create(self, process_id: str, *, retain: bool = False) -> WorkspaceHandle:
        return WorkspaceHandle("opaque", process_id, "minimal")

    def inspect(self, handle: WorkspaceHandle) -> WorkspaceInfo:
        return WorkspaceInfo(handle, False, frozenset())

    def snapshot(self, handle: WorkspaceHandle) -> Snapshot:
        raise UnsupportedWorkspaceOperation("snapshot")

    def diff(self, handle: WorkspaceHandle, baseline: Snapshot) -> WorkspaceDiff:
        raise UnsupportedWorkspaceOperation("diff")

    def destroy(self, handle: WorkspaceHandle) -> None:
        pass


def test_protocol_and_explicit_unsupported():
    provider: WorkspaceProvider = MinimalProvider()
    assert isinstance(provider, WorkspaceProvider)
    handle = provider.create("process")
    assert provider.inspect(handle).handle == handle
    assert not hasattr(handle, "path")
    with pytest.raises(UnsupportedWorkspaceOperation):
        provider.snapshot(handle)
    provider.destroy(handle)
