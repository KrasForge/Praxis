"""TM-3: actual native workloads cannot reach host or sibling data."""
import asyncio
import sys

from praxis.executors.local import LocalProcessExecutor
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_host_sibling_symlink_and_network_isolation(tmp_path):
    async def exercise():
        provider = LocalWorkspaces(tmp_path / "work")
        owner = provider.create("p")
        sibling = provider.create("q")
        path = provider.path_for(owner, "p")
        private = provider.path_for(sibling, "q") / "private"
        private.write_text("sibling-canary")
        host = tmp_path / "host"
        host.write_text("host-canary")
        (path / "escape").symlink_to(host)
        code = """import pathlib,socket,sys
for name in sys.argv[1:]:
    try:
        pathlib.Path(name).read_text()
    except (FileNotFoundError, PermissionError):
        pass
    else:
        raise AssertionError('host read escaped')
pathlib.Path('own').write_text('allowed')
s = socket.socket()
try:
    s.connect(('192.0.2.1', 80))
except OSError:
    pass
else:
    raise AssertionError('network escaped')
print('isolated')
"""
        spec = ProcessSpec("attack", "local", inputs={"argv": [sys.executable, "-c", code, str(private), str(host), "escape", str(path / ".." / sibling.workspace_id / "private")], "timeout": 5})
        executor = LocalProcessExecutor(provider)
        request = ExecutionRequest("p", "a", spec, owner.workspace_id, path)
        assert (await executor.start(request)).applied
        result = await executor.collect_result("a")
        assert result.exit_code == 0, result.stderr
        assert result.stdout == "isolated\n" and (path / "own").read_text() == "allowed"
        assert host.read_text() == "host-canary"
    asyncio.run(exercise())


def test_no_implicit_mount_for_requested_host_executable(tmp_path):
    from praxis.executors.isolation import LinuxIsolation
    command = LinuxIsolation().command([str(tmp_path / "malicious")], tmp_path)
    assert "--ro-bind" in command
    assert command.count(str(tmp_path / "malicious")) == 1


def test_isolation_missing_fails_closed(tmp_path, monkeypatch):
    import pytest
    from praxis.executors.isolation import LinuxIsolation
    monkeypatch.setattr("praxis.executors.isolation.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="isolation_unavailable"):
        LinuxIsolation().command(["true"], tmp_path)
