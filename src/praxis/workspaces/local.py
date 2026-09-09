"""Local directory workspaces. This provider is not an OS process sandbox."""

import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from praxis.workspaces.protocol import (
    Snapshot, UnsupportedWorkspaceOperation, WorkspaceDiff, WorkspaceError,
    WorkspaceHandle, WorkspaceInfo,
)


class LocalWorkspaces:
    protocol_version = 1

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data = self.root / "data"
        self.records = self.root / "records"
        self.data.mkdir(exist_ok=True, mode=0o700)
        self.records.mkdir(exist_ok=True, mode=0o700)

    def create(self, process_id: str, *, retain: bool = False) -> WorkspaceHandle:
        handle = WorkspaceHandle(str(uuid4()), process_id, "local")
        path = self.data / handle.workspace_id
        path.mkdir(mode=0o700)
        try:
            record = {"process_id": process_id, "retain": retain}
            (self.records / f"{handle.workspace_id}.json").write_text(json.dumps(record))
        except BaseException:
            path.rmdir()
            raise
        return handle

    def inspect(self, handle: WorkspaceHandle) -> WorkspaceInfo:
        self.path_for(handle, handle.process_id)
        record = json.loads((self.records / f"{handle.workspace_id}.json").read_text())
        return WorkspaceInfo(handle, record["retain"], frozenset())

    def path_for(self, handle: WorkspaceHandle, process_id: str) -> Path:
        try:
            UUID(handle.workspace_id)
            if handle.provider != "local" or handle.process_id != process_id:
                raise WorkspaceError("workspace ownership mismatch")
            record = json.loads((self.records / f"{handle.workspace_id}.json").read_text())
            if record["process_id"] != process_id:
                raise WorkspaceError("workspace ownership mismatch")
            path = self.data / handle.workspace_id
            if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.data:
                raise WorkspaceError("workspace path unavailable or escaped")
            return path
        except (OSError, ValueError, KeyError) as exc:
            raise WorkspaceError("invalid or unavailable workspace") from exc

    def snapshot(self, handle: WorkspaceHandle) -> Snapshot:
        raise UnsupportedWorkspaceOperation("snapshot")

    def diff(self, handle: WorkspaceHandle, baseline: Snapshot) -> WorkspaceDiff:
        raise UnsupportedWorkspaceOperation("diff")

    def destroy(self, handle: WorkspaceHandle) -> None:
        path = self.path_for(handle, handle.process_id)
        shutil.rmtree(path)
        (self.records / f"{handle.workspace_id}.json").unlink()

    def cleanup(self, handle: WorkspaceHandle) -> bool:
        if self.inspect(handle).retained:
            return False
        self.destroy(handle)
        return True
