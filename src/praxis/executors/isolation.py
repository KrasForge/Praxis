"""TM-3: empty-root Linux namespace with a host-owned runtime allowlist."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LinuxIsolation:
    # Runtime images are host policy, never inferred from untrusted argv paths.
    runtime_roots: tuple[Path, ...] = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"), Path(sys.base_prefix))

    def command(self, argv: list[str], workspace: Path) -> list[str]:
        binary = shutil.which("bwrap")
        if sys.platform != "linux" or binary is None:
            raise ValueError("isolation_unavailable")
        if workspace.is_symlink() or not workspace.is_dir() or workspace != workspace.resolve():
            raise ValueError("invalid_isolation_workspace")
        command = [binary, "--unshare-all", "--die-with-parent", "--cap-drop", "ALL"]
        seen = set()
        for configured in self.runtime_roots:
            if not configured.exists():
                continue
            root = configured.resolve()
            if root == Path("/") or root == workspace or root in workspace.parents or workspace in root.parents:
                raise ValueError("unsafe_runtime_mount")
            if root not in seen:
                command.extend(["--ro-bind", str(root), str(root)])
                seen.add(root)
            if configured != root:
                command.extend(["--symlink", str(root), str(configured)])
        command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                        "--bind", str(workspace), str(workspace), "--chdir", str(workspace)])
        args = list(argv)
        if args[0] == sys.executable:
            args[0] = str(Path(sys.executable).resolve())
        return [*command, "--", *args]

    def executable_path(self, executable: str) -> tuple[Path | None, Path]:
        """Resolve ``executable`` the way the sandbox will, one symlink hop at a time.

        Returns ``(path, where)``: ``path`` is the file the sandbox reaches, or ``None`` when
        resolution leaves the runtime mounts (for example ``/usr/bin/python3`` ->
        ``/etc/alternatives/python3`` on Debian-family hosts); ``where`` names the reached
        file or the first path outside the mounts, for diagnostics.
        """
        if executable == sys.executable:
            executable = str(Path(sys.executable).resolve())
        located = executable if "/" in executable else shutil.which(executable) or executable
        start = Path(os.path.abspath(located))
        visible = []
        for configured in self.runtime_roots:
            if configured.exists():
                visible.append(configured.resolve())
                if configured != configured.resolve():
                    visible.append(configured)  # bwrap recreates the alias symlink
        pending = list(start.parts[1:])
        current = Path("/")
        hops = 0
        while pending:
            current = current / pending.pop(0)
            if not any(current == root or root in current.parents or current in root.parents for root in visible):
                return None, current.joinpath(*pending)
            if current.is_symlink():
                hops += 1
                if hops > 40:
                    return None, current
                target = Path(os.readlink(current))
                base = Path(os.path.normpath(target if target.is_absolute() else current.parent / target))
                pending = list(base.parts[1:]) + pending
                current = Path("/")
        if not current.is_file() or not any(current == root or root in current.parents for root in visible):
            return None, current
        return current, current
