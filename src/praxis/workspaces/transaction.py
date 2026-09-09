"""Atomic publication of verified work into managed canonical revisions.

Canonical writers use this API; readers resolve path for each revision. Older
revision directories remain available after the current pointer is replaced.
"""

import fcntl
import os
import shutil
from pathlib import Path
from uuid import uuid4

from praxis.kernel.events import Event
from praxis.validators.policy import VerificationReport
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError, WorkspaceHandle


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
            if canonical.revision != self.baseline_revision:
                raise WorkspaceError("canonical_conflict")
            revision = str(uuid4())
            version = canonical.versions / revision
            version.mkdir()
            source = self.provider.path_for(self.handle, self.handle.process_id)
            shutil.copytree(source, version / "tree", symlinks=True)
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
