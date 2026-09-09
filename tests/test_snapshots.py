from dataclasses import replace

import pytest

from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError


def test_snapshot_diff_binary_text_modes_and_directories(tmp_path):
    provider = LocalWorkspaces(tmp_path)
    handle = provider.create("p")
    path = provider.path_for(handle, "p")
    (path / "text").write_text("old")
    (path / "binary").write_bytes(b"\x00\xff")
    (path / "empty").mkdir()
    baseline = provider.snapshot(handle)
    assert provider.snapshot(handle) == baseline
    (path / "text").write_text("new")
    (path / "binary").unlink()
    (path / "new").write_bytes(b"\x80")
    (path / "empty").chmod(0o700)
    diff = provider.diff(handle, baseline)
    assert diff.added == ("new",)
    assert "text" in diff.changed
    assert diff.removed == ("binary",)
    assert dict(baseline.files)["binary"]
    with pytest.raises(WorkspaceError):
        provider.diff(handle, replace(baseline, snapshot_id="forged"))


def test_symlink_rejected(tmp_path):
    provider = LocalWorkspaces(tmp_path / "root")
    handle = provider.create("p")
    (provider.path_for(handle, "p") / "escape").symlink_to(tmp_path)
    with pytest.raises(WorkspaceError):
        provider.snapshot(handle)
