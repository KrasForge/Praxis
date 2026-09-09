"""Local directory workspaces. This provider is not an OS process sandbox."""

import hashlib
import json
import os
import stat
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from praxis.workspaces.protocol import (
    Snapshot, WorkspaceDiff, WorkspaceError,
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
        return WorkspaceInfo(handle, record["retain"], frozenset({"snapshot", "diff"}))

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
        path = self.path_for(handle, handle.process_id)
        blobs = self.root / "blobs"
        blobs.mkdir(exist_ok=True, mode=0o700)
        entries: list[tuple[str, str]] = []
        for item in sorted(path.rglob("*")):
            metadata = item.lstat()
            if item.is_symlink() or not item.resolve().is_relative_to(path):
                raise WorkspaceError("snapshot cannot contain symlinks")
            relative = item.relative_to(path).as_posix()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                content = b"directory"
                relative += "/"
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(item, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    content = stream.read()
                    after = os.fstat(stream.fileno())
                if (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns
                ):
                    raise WorkspaceError("workspace changed during snapshot")
            else:
                raise WorkspaceError("snapshot cannot contain special files")
            blob = str(mode).encode() + b"\n" + content
            digest = hashlib.sha256(blob).hexdigest()
            target = blobs / digest
            if not target.exists():
                target.write_bytes(blob)
            entries.append((relative, digest))
        manifest = json.dumps(entries, separators=(",", ":")).encode()
        identity = hashlib.sha256(manifest).hexdigest()
        snapshots = self.root / "snapshots"
        snapshots.mkdir(exist_ok=True, mode=0o700)
        (snapshots / identity).write_bytes(manifest)
        return Snapshot(handle.workspace_id, identity, tuple(entries))

    def diff(self, handle: WorkspaceHandle, baseline: Snapshot) -> WorkspaceDiff:
        if baseline.workspace_id != handle.workspace_id:
            raise WorkspaceError("snapshot workspace mismatch")
        expected = hashlib.sha256(json.dumps(
            baseline.files, separators=(",", ":")
        ).encode()).hexdigest()
        if expected != baseline.snapshot_id:
            raise WorkspaceError("corrupt snapshot")
        before = dict(baseline.files)
        after = dict(self.snapshot(handle).files)
        return WorkspaceDiff(
            tuple(sorted(after.keys() - before.keys())),
            tuple(sorted(k for k in before.keys() & after.keys() if before[k] != after[k])),
            tuple(sorted(before.keys() - after.keys())),
        )

    def destroy(self, handle: WorkspaceHandle) -> None:
        path = self.path_for(handle, handle.process_id)
        shutil.rmtree(path)
        (self.records / f"{handle.workspace_id}.json").unlink()

    def cleanup(self, handle: WorkspaceHandle) -> bool:
        if self.inspect(handle).retained:
            return False
        self.destroy(handle)
        return True
