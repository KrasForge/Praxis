# Repository operating contract

This file applies to the complete Praxis repository.

- Read `README.md`, `CONTRIBUTING.md`, and the relevant document under `docs/`
  before changing a runtime boundary. Add durable decisions under `docs/adr/`.
- Run `mise run check` before declaring a change complete. A focused test is
  appropriate while iterating, but structural implementation alone is not
  acceptance evidence.
- Preserve dirty worktrees and existing user changes. Do not rewrite history,
  push, publish, deploy, restore, or delete persistent state unless explicitly
  authorized.
- Never commit credentials, provider tokens, private keys, recovery material,
  workload secrets, or real production data.
- Treat `.venv`, caches, coverage, build output, temporary workspaces, runtime
  databases, and generated qualification output as generated unless the release
  procedure explicitly names an artifact.
- Keep the kernel harness-independent and fail closed at authority, verification,
  effect, replay, and compatibility boundaries. Unknown wire fields remain an
  error unless a versioned compatibility decision changes that rule.
