# Praxis

Praxis owns execution semantics: processes, authority, verification, and recovery.
Modulo owns the human-facing UX; Noesis owns knowledge. Model-specific integrations
belong in executor adapters, never in the kernel.

Python 3.11 or newer is supported. Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy
```

`uv sync` installs the package in editable mode. Kernel code lives in
`src/praxis/kernel`, harness adapters in `src/praxis/executors`, API and transport
code in `src/praxis/api`, and tests outside the package in `tests`.
