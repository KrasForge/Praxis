# Contributing to Praxis

## Setup and checks

Install the locked development environment with `uv sync --locked`. Run the
common acceptance gate with `mise run check`; it executes the same test, Ruff,
and strict mypy checks used by CI. `uv run python scripts/qualify.py` is the
fresh-wheel release gate and is required for packaging or release changes.

Use the smallest relevant `uv run pytest tests/test_…py` command while
iterating, then run the common gate. Evidence should name the command and its
result. A new class or successful import is not evidence that a lifecycle,
authority, recovery, or publication path works.

## Change contract

- Keep public wire and storage changes versioned and backward-compatibility
  behavior explicit.
- Add regression tests for bug fixes and adversarial tests for trust-boundary
  changes.
- Put durable architecture decisions in `docs/adr/NNNN-short-title.md`; do not
  rewrite an accepted decision—supersede it.
- Document configuration variable names and safe defaults. Never commit live
  tokens, secret values, private keys, provider payloads, or production data.
- Do not commit `.venv`, caches, coverage, build output, temporary workspaces,
  runtime SQLite files, or local qualification environments.
- Use reviewable branches and commits. Do not push, publish a package, tag a
  release, deploy, or rewrite shared history without explicit authorization.
