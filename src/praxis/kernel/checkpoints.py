"""Compatible executor/workspace checkpoint capture and restoration."""

import hashlib
import json
import shutil
from pathlib import PurePosixPath

from praxis.executors.protocol import CheckpointResult, ControlResult, ExecutionRequest, Executor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process
from praxis.storage.protocol import ProcessStore, StoredCheckpoint
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError, WorkspaceHandle


class CheckpointManager:
    def __init__(self, store: ProcessStore, workspaces: LocalWorkspaces, authority: Authority):
        self.store = store
        self.workspaces = workspaces
        self.authority = authority

    async def capture(self, process: Process, executor: Executor, handle: WorkspaceHandle) -> CheckpointResult:
        self.authority.require(process.process_id, Resource.EXECUTOR, "control", process.spec.executor)
        self.authority.require(process.process_id, Resource.WORKSPACE, "snapshot", process.process_id)
        if process.state not in (State.RUNNING, State.SUSPENDED):
            return CheckpointResult(ControlResult(True, False, "process_not_running"))
        if "checkpoint" not in executor.descriptor.features:
            return CheckpointResult(ControlResult(False, False, "checkpoint_unavailable"))
        result = await executor.checkpoint(process.attempt_id)
        item = result.checkpoint
        if not result.control.applied:
            return result
        if item is None or (item.process_id, item.attempt_id, item.executor, item.protocol_version) != (
            process.process_id, process.attempt_id, executor.descriptor.name, executor.descriptor.protocol_version
        ):
            return CheckpointResult(ControlResult(True, False, "checkpoint_identity_mismatch"))
        snapshot = self.workspaces.snapshot(handle)
        self.store.save_checkpoint(StoredCheckpoint(item, snapshot.snapshot_id))
        self.store.save(process, (Event(process.process_id, "checkpoint.saved", {
            "snapshot_id": snapshot.snapshot_id, "attempt_id": process.attempt_id,
            "executor": item.executor,
        }, parent_id=process.parent_id),))
        return result

    async def restore(self, process: Process, executor: Executor, handle: WorkspaceHandle) -> ControlResult:
        self.authority.require(process.process_id, Resource.EXECUTOR, "control", process.spec.executor)
        self.authority.require(process.process_id, Resource.WORKSPACE, "snapshot", process.process_id)
        stored = self.store.load_checkpoint(process.process_id)
        if stored is None:
            return ControlResult(True, False, "checkpoint_not_found")
        if "restore" not in executor.descriptor.features:
            return ControlResult(False, False, "restore_unavailable")
        item = stored.checkpoint
        if (item.executor, item.protocol_version, item.process_id, item.attempt_id) != (
            executor.descriptor.name, executor.descriptor.protocol_version, process.process_id, process.attempt_id
        ):
            return ControlResult(True, False, "incompatible_checkpoint")
        baseline = self.workspaces.snapshot(handle)
        try:
            self.restore_workspace(handle, stored.workspace_snapshot_id)
            request = ExecutionRequest(process.process_id, process.attempt_id, process.spec,
                                       handle.workspace_id, self.workspaces.path_for(handle, process.process_id))
            result = await executor.restore(request, item)
            if not result.applied:
                self.restore_workspace(handle, baseline.snapshot_id)
            return result
        except Exception:
            self.restore_workspace(handle, baseline.snapshot_id)
            return ControlResult(True, False, "checkpoint_restore_error")

    def restore_workspace(self, handle: WorkspaceHandle, snapshot_id: str) -> None:
        if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise WorkspaceError("invalid snapshot identity")
        manifest = (self.workspaces.root / "snapshots" / snapshot_id).read_bytes()
        if hashlib.sha256(manifest).hexdigest() != snapshot_id:
            raise WorkspaceError("corrupt snapshot manifest")
        entries = json.loads(manifest)
        contents: list[tuple[str, int, bytes]] = []
        for relative, digest in entries:
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or str(path) != relative.rstrip("/") or str(path) == ".":
                raise WorkspaceError("escaped snapshot path")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise WorkspaceError("invalid blob identity")
            blob = (self.workspaces.root / "blobs" / digest).read_bytes()
            if hashlib.sha256(blob).hexdigest() != digest:
                raise WorkspaceError("corrupt snapshot blob")
            mode, content = blob.split(b"\n", 1)
            contents.append((relative, int(mode), content))
        root = self.workspaces.path_for(handle, handle.process_id)
        for item in root.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        for relative, permissions, content in contents:
            target = root / relative
            if relative.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(permissions)
        for relative, permissions, _ in reversed(contents):
            if relative.endswith("/"):
                (root / relative).chmod(permissions)
