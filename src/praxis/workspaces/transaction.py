"""Atomic publication of verified work into managed canonical revisions.

Canonical writers use this API; readers resolve path for each revision. Older
revision directories remain available after the current pointer is replaced.
"""

import fcntl
import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from praxis.kernel.events import Event
from praxis.validators.policy import VerificationReport
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import Snapshot, WorkspaceError, WorkspaceHandle


class CanonicalDirectory:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = self.root / "versions"
        self.versions.mkdir(exist_ok=True)
        self.pointer = self.root / "current"
        with (self.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.pointer.is_symlink():
                if self.pointer.exists():
                    raise WorkspaceError("canonical pointer must be a symlink")
                revision = str(uuid4())
                (self.versions / revision / "tree").mkdir(parents=True)
                self.pointer.symlink_to(Path("versions") / revision / "tree")

    @property
    def path(self) -> Path:
        path = self.pointer.resolve(strict=True)
        if path.parent.parent != self.versions or path.name != "tree":
            raise WorkspaceError("canonical pointer escaped")
        return path

    @property
    def revision(self) -> str:
        return self.path.parent.name


class WorkspaceTransaction:
    def __init__(self, provider: LocalWorkspaces, handle: WorkspaceHandle, canonical: CanonicalDirectory):
        self.provider = provider
        self.handle = handle
        self.canonical = canonical
        self.events: list[Event] = []
        self.committed = False
        staged = provider.path_for(handle, handle.process_id)
        if any(staged.iterdir()):
            raise WorkspaceError("transaction requires an empty staged workspace")
        with (canonical.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.baseline_revision = canonical.revision
            shutil.copytree(canonical.path, staged, dirs_exist_ok=True, symlinks=True)
            self.baseline = provider.snapshot(handle)

    def commit(self, report: VerificationReport) -> Event:
        if self.committed:
            raise WorkspaceError("transaction already committed")
        snapshot = self.provider.snapshot(self.handle)
        if report.approved is not True or report.snapshot_id != snapshot.snapshot_id:
            raise WorkspaceError("commit requires verification of current snapshot")
        canonical = self.canonical
        with (canonical.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if canonical.revision != self.baseline_revision or self._snapshot_tree(canonical.path).snapshot_id != self.baseline.snapshot_id:
                raise WorkspaceError("canonical_conflict")
            revision = str(uuid4())
            version = canonical.versions / revision
            version.mkdir()
            source = self.provider.path_for(self.handle, self.handle.process_id)
            shutil.copytree(source, version / "tree", symlinks=True)
            if self._snapshot_tree(version / "tree").snapshot_id != snapshot.snapshot_id:
                raise WorkspaceError("staged_workspace_changed")
            event = Event(self.handle.process_id, "workspace.committed", {
                "workspace_id": self.handle.workspace_id, "snapshot_id": snapshot.snapshot_id,
                "revision": revision, "previous_revision": self.baseline_revision,
            })
            (version / "commit.json").write_text(event.to_json())
            for file in version.rglob("*"):
                if file.is_file():
                    with file.open("rb") as stream:
                        os.fsync(stream.fileno())
            pointer = canonical.root / f".publish-{revision}"
            pointer.symlink_to(Path("versions") / revision / "tree")
            os.replace(pointer, canonical.pointer)
            self.committed = True
            directory = os.open(canonical.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.committed = True
            self.events.append(event)
            return event

    def receipt(self) -> Event | None:
        path = self.canonical.path.parent / "commit.json"
        return Event.from_json(path.read_text()) if path.exists() else None

    def _snapshot_tree(self, path: Path) -> Snapshot:
        handle = self.provider.create(self.handle.process_id)
        try:
            target = self.provider.path_for(handle, handle.process_id)
            shutil.copytree(path, target, symlinks=True, dirs_exist_ok=True)
            return self.provider.snapshot(handle)
        finally:
            self.provider.destroy(handle)

    def rollback(self) -> Event:
        if self.committed:
            raise WorkspaceError("published revisions cannot be rolled back implicitly")
        staged = self.provider.path_for(self.handle, self.handle.process_id)
        blobs: list[tuple[str, int, bytes]] = []
        for relative, digest in self.baseline.files:
            blob = (self.provider.root / "blobs" / digest).read_bytes()
            if hashlib.sha256(blob).hexdigest() != digest:
                raise WorkspaceError("corrupt rollback snapshot")
            raw_mode, content = blob.split(b"\n", 1)
            blobs.append((relative, int(raw_mode), content))
        for path in staged.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        for relative, mode, content in blobs:
            target = staged / relative
            if relative.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(mode)
        for relative, mode, _ in reversed(blobs):
            if relative.endswith("/"):
                (staged / relative).chmod(mode)
        event = Event(self.handle.process_id, "workspace.rolled_back", {
            "workspace_id": self.handle.workspace_id, "snapshot_id": self.baseline.snapshot_id,
        })
        self.events.append(event)
        return event
