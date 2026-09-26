"""``git_commit`` effects: commit a configured local repository, never push (ADR 0001).

The effect target names a repository from the adapter configuration, never a path, so a
workload cannot point the adapter at an arbitrary directory. The commit is refused when
``HEAD`` moved from ``expected_head``. The idempotency key is written as a commit trailer,
which lets ``lookup`` find a commit that was made before a crash. Hooks, signing and
user-level git configuration are disabled; pushing stays a separate decision.
"""

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path

from praxis.kernel.effect_service import EffectReceipt
from praxis.kernel.effects import Effect, EffectKind

TRAILER = "Praxis-Effect"


class GitError(RuntimeError):
    pass


class GitCommitAdapter:
    kind = EffectKind.GIT_COMMIT

    def __init__(self, repositories: Mapping[str, str], *, author_name: str = "Praxis",
                 author_email: str = "praxis@localhost", timeout: float = 30.0, git: str = "git"):
        if not repositories:
            raise ValueError("at least one repository required")
        self.repositories = {name: Path(path).resolve() for name, path in repositories.items()}
        self.author = (author_name, author_email)
        self.timeout = timeout
        self.git = git

    async def apply(self, effect: Effect) -> EffectReceipt:
        repository = self._repository(effect)
        if repository is None:
            return EffectReceipt(False, "unknown_repository")
        head = await self._head(repository)
        if head != effect.payload["expected_head"]:
            return EffectReceipt(False, "head_moved")
        await self._run(repository, "add", "--all")
        staged = await self._run(repository, "diff", "--cached", "--quiet", check=False)
        if staged[0] == 0:
            return EffectReceipt(False, "nothing_to_commit")
        message = f"{effect.payload['message'].rstrip()}\n\n{TRAILER}: {effect.idempotency_key}\n"
        await self._run(repository, "commit", "--quiet", "--no-verify", "--file", "-", stdin=message)
        return EffectReceipt(True, "committed", await self._head(repository))

    async def lookup(self, idempotency_key: str) -> EffectReceipt | None:
        for repository in self.repositories.values():
            code, output = await self._run(repository, "log", "--format=%H", "--fixed-strings",
                                           f"--grep={TRAILER}: {idempotency_key}", "-n", "1", check=False)
            if code == 0 and output.strip():
                return EffectReceipt(True, "committed", output.strip())
        # A local commit is atomic: no trailer means the commit never happened.
        return EffectReceipt(False, "not_committed")

    def _repository(self, effect: Effect) -> Path | None:
        return self.repositories.get(effect.target)

    async def _head(self, repository: Path) -> str:
        code, output = await self._run(repository, "rev-parse", "--verify", "HEAD", check=False)
        return output.strip() if code == 0 else ""

    async def _run(self, repository: Path, *args: str, stdin: str | None = None,
                   check: bool = True) -> tuple[int, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repository),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "GIT_AUTHOR_NAME": self.author[0], "GIT_AUTHOR_EMAIL": self.author[1],
               "GIT_COMMITTER_NAME": self.author[0], "GIT_COMMITTER_EMAIL": self.author[1], "LC_ALL": "C"}
        process = await asyncio.create_subprocess_exec(
            self.git, "-C", str(repository), "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false",
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(None if stdin is None else stdin.encode()),
                                               self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitError("git_timeout") from None
        code = process.returncode if process.returncode is not None else -1
        if check and code != 0:
            raise GitError(f"git {args[0]} failed")
        return code, stdout.decode(errors="replace")
