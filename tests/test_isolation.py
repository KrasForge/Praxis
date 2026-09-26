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


def test_executable_resolution_follows_every_symlink_hop(tmp_path):
    from praxis.executors.isolation import LinuxIsolation
    runtime, outside = tmp_path / "runtime", tmp_path / "outside"
    (runtime / "bin").mkdir(parents=True)
    outside.mkdir()
    (runtime / "bin" / "real").write_text("#!/bin/true\n")
    (outside / "tool").write_text("#!/bin/true\n")
    (runtime / "bin" / "relative").symlink_to("real")
    (runtime / "bin" / "absolute").symlink_to(runtime / "bin" / "real")
    # Debian alternatives shape: the final target is mounted, an intermediate hop is not.
    (outside / "alternative").symlink_to(runtime / "bin" / "real")
    (runtime / "bin" / "via-outside").symlink_to(outside / "alternative")
    (runtime / "bin" / "escape").symlink_to(outside / "tool")
    (runtime / "bin" / "loop").symlink_to("loop")
    isolation = LinuxIsolation(runtime_roots=(runtime,))
    real = runtime / "bin" / "real"
    assert isolation.executable_path(str(real)) == (real, real)
    assert isolation.executable_path(str(runtime / "bin" / "relative"))[0] == real
    assert isolation.executable_path(str(runtime / "bin" / "absolute"))[0] == real
    assert isolation.executable_path(str(runtime / "bin" / "via-outside")) == (None, outside / "alternative")
    assert isolation.executable_path(str(runtime / "bin" / "escape")) == (None, outside / "tool")
    assert isolation.executable_path(str(runtime / "bin" / "loop"))[0] is None
    assert isolation.executable_path(str(runtime / "bin" / "missing"))[0] is None
    assert isolation.executable_path(sys.executable)[0] is None  # not under these roots
    assert LinuxIsolation().executable_path(sys.executable)[0] is not None  # default roots


def test_unreachable_executable_fails_before_launch(tmp_path, caplog):
    import shutil

    from praxis.executors.isolation import LinuxIsolation
    runtime, outside = tmp_path / "runtime", tmp_path / "outside"
    runtime.mkdir()
    outside.mkdir()
    shutil.copy("/bin/true" if not sys.platform.startswith("win") else sys.executable, outside / "tool")
    (runtime / "tool").symlink_to(outside / "tool")

    async def exercise():
        provider = LocalWorkspaces(tmp_path / "work")
        handle = provider.create("p")
        executor = LocalProcessExecutor(provider, isolation=LinuxIsolation(runtime_roots=(runtime,)))
        spec = ProcessSpec("run", "local", inputs={"argv": [str(runtime / "tool")]})
        control = await executor.start(ExecutionRequest("p", "a", spec, handle.workspace_id,
                                                        provider.path_for(handle, "p")))
        assert (control.applied, control.reason) == (False, "executable_not_in_sandbox")
        assert "a" not in executor.processes  # nothing was launched
    asyncio.run(exercise())
    assert str(outside / "tool") in caplog.text
