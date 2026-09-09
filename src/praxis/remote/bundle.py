"""Bounded workspace transfer containing regular files and directories only."""

import base64
import json
import shutil
from dataclasses import dataclass
from pathlib import PurePosixPath

from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


@dataclass(frozen=True)
class WorkspaceBundle:
    entries: tuple[tuple[str, str | None, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or len(self.entries) > 4096:
            raise ValueError("workspace_bundle_too_large")
        names = set()
        files = set()
        size = 0
        for entry in self.entries:
            if not isinstance(entry, tuple) or len(entry) != 3:
                raise ValueError("invalid_bundle_entry")
            name, content, mode = entry
            if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
                raise ValueError("invalid_bundle_path")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or str(path) != name or name == "." or name in names:
                raise ValueError("invalid_bundle_path")
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise ValueError("invalid_bundle_mode")
            names.add(name)
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("invalid_bundle_content")
                size += len(base64.b64decode(content, validate=True))
                files.add(name)
        if size > 8 * 1024 * 1024:
            raise ValueError("workspace_bundle_too_large")
        if any(str(parent) in files for name in names for parent in PurePosixPath(name).parents):
            raise ValueError("bundle_file_directory_conflict")

    def to_json(self) -> str:
        return json.dumps({"entries": self.entries}, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "WorkspaceBundle":
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"entries"} or not isinstance(data["entries"], list):
            raise ValueError("invalid_workspace_bundle")
        return cls(tuple(tuple(entry) for entry in data["entries"]))

    @classmethod
    def capture(cls, provider: LocalWorkspaces, handle: WorkspaceHandle) -> "WorkspaceBundle":
        snapshot = provider.snapshot(handle)
        root = provider.path_for(handle, handle.process_id)
        entries = []
        for name, _ in snapshot.files:
            path = root / name
            entries.append((name.rstrip("/"), None if name.endswith("/") else base64.b64encode(path.read_bytes()).decode(),
                            path.stat().st_mode & 0o777))
        return cls(tuple(entries))

    def restore(self, provider: LocalWorkspaces, handle: WorkspaceHandle) -> None:
        # Validate the current tree before replacing it; never follow existing symlinks.
        provider.snapshot(handle)
        root = provider.path_for(handle, handle.process_id)
        for path in root.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for name, content, mode in sorted(self.entries, key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.mkdir(exist_ok=True)
            else:
                path.write_bytes(base64.b64decode(content, validate=True))
            path.chmod(mode)
