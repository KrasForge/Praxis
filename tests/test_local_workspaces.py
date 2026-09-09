from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError, WorkspaceProvider


def test_parallel_isolation_and_cleanup(tmp_path):
    provider: WorkspaceProvider = LocalWorkspaces(tmp_path)
    assert isinstance(provider, WorkspaceProvider)
    with ThreadPoolExecutor() as pool:
        handles = list(pool.map(provider.create, ["a", "b", "c"]))
    paths = [provider.path_for(h, h.process_id) for h in handles]
    assert len(set(paths)) == 3
    (paths[0] / "private").write_text("a")
    assert not (paths[1] / "private").exists()
    retained = provider.create("retained", retain=True)
    assert not LocalWorkspaces(tmp_path).cleanup(retained)
    assert provider.cleanup(handles[0])
    assert not paths[0].exists()


def test_forged_and_symlink_workspace(tmp_path):
    provider = LocalWorkspaces(tmp_path / "workspaces")
    handle = provider.create("owner")
    with pytest.raises(WorkspaceError):
        provider.path_for(handle, "other")
    with pytest.raises(WorkspaceError):
        provider.path_for(replace(handle, process_id="other"), "other")
    path = provider.path_for(handle, "owner")
    path.rmdir()
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        provider.path_for(handle, "owner")
