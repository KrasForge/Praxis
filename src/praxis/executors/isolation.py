"""TM-3: empty-root Linux namespace with a host-owned runtime allowlist."""

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
