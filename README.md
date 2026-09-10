# Praxis

**A process execution kernel that refuses to call unverified work successful.**

Praxis runs work — native programs, shell commands, agent adapters, remote workers —
and owns the semantics around it: authority, verification, transactional publication
and recovery. A process that exits `0` is not a process that succeeded. Praxis keeps
those two facts apart in the type system, in the persisted record, and at the commit
boundary, so a failed or unverified result can never reach a canonical artifact.

Praxis is a library and an ASGI service, not an application. Modulo owns the
human-facing UX; Noesis owns knowledge. Model-specific integrations live in executor
adapters, never in the kernel.

- **Status:** v1.0.0 — v1 wire contracts, SQLite layout 2. See the
  [qualification report](docs/qualification.md) for coverage and deployment limits.
- **Requires:** Python 3.11–3.14. Local sandboxed execution requires Linux and
  [Bubblewrap](https://github.com/containers/bubblewrap).
- **Dependencies:** none at runtime. The kernel, API, client and adapters are pure
  standard library; model SDKs and ASGI servers are host dependencies.

## Install

```sh
uv sync --locked          # editable install with dev tooling
uv run python examples/demo.py
```

`examples/demo.py` runs offline — no model calls, no external publication — and
exercises local execution, verified commit, graphs, an agent-adapter fixture, Noesis
contract mapping, control-plane inspection and restart recovery.

## Quickstart

Run a program, require an artifact, publish it only if verification approves:

```python
import asyncio, sys
from pathlib import Path

from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory


async def main(root: Path) -> None:
    workspaces = LocalWorkspaces(root / "workspaces")
    authority = Authority(execution_defaults=frozenset({"local"}))
    kernel = Kernel(SQLiteStore(root / "runtime.db"), workspaces,
                    {"local": LocalProcessExecutor(workspaces)}, authority=authority)
    canonical = CanonicalDirectory(root / "canonical")

    spec = ProcessSpec(
        objective="write the answer",
        executor="local",
        inputs={"argv": [sys.executable, "-c",
                         "from pathlib import Path; Path('answer.txt').write_text('42')"]},
        contract={"required_outputs": ["answer.txt"]},
    )
    process = kernel.create(spec, canonical=canonical)

    # Authority is issued by the kernel, never carried in a spec.
    authority.issue(process.process_id, Resource.FILESYSTEM,
                    frozenset({"read", "write"}), str(canonical.root))
    authority.issue(process.process_id, Resource.WORKSPACE,
                    frozenset({"commit"}), process.process_id)

    kernel.start(process.process_id)
    await kernel.tasks[process.process_id]

    result = kernel.result(process.process_id)
    print(result.state.value, result.outcome.status.value, result.verification.approved)
    print((canonical.path / "answer.txt").read_text())


asyncio.run(main(Path("./praxis-quickstart")))
```

```
completed completed True
42
```

The process ran in a private, sandboxed workspace. Its output was snapshotted,
checked against the contract, and only then committed to the canonical directory.

### The invariant, concretely

Now replace the program with one that writes nothing — `"pass"` — and change nothing
else. It still exits `0`, so the executor still reports `completed`:

```
failed completed False
```

`result.verification.missing_outputs` is `('answer.txt',)`, and the canonical
directory is empty. The executor completed; the *process* failed, because
verification did not approve it, so nothing was published. That distinction —
`state` vs `outcome.status` — is the core of the kernel, and it holds across restart,
retry, remote dispatch and recovery.

## Core model

| Concept | What it is |
| --- | --- |
| `ProcessSpec` | Versioned submission: objective, executor, inputs, environment, contract, budget, context, metadata. Closed schema; unknown fields are rejected. |
| `Outcome` | What the executor did: `completed`, `failed`, `cancelled`, `timed_out`, `partial`, `budget_exhausted`, `unavailable`. |
| `ProcessResult` | What the kernel concluded: terminal `state`, the outcome, independent `verification`, artifacts, evidence, claims, effects, usage. |
| `Contract` | Machine-checkable acceptance: required outputs, invariants, validators, quorum. |
| Workspace | A private staging directory. Snapshots identify immutable bytes; validators run on disposable copies. |
| `CanonicalDirectory` | A managed version store whose `current` pointer moves atomically, only after verification, baseline and authority checks. |
| `Capability` | Typed, kernel-issued authority over filesystem, network, executor, workspace, secret or effect scopes. Delegation may only narrow. |
| Effect | An external write (git commit, message, publication) staged and approved separately from execution, with idempotency receipts. |
| `ProcessGraph` | DAG dependencies with success/terminal requirements and block/fail/continue policies, persisted via `GraphStore`. |

Read [concepts](docs/concepts.md) for how these fit together, and
[architecture](docs/architecture.md) for failure semantics.

## Executors

Register any mapping of name to executor with the kernel. Features are advertised,
not assumed — a matching protocol version does not imply cancellation or checkpoint
support, and the kernel refuses wall-limited work on an adapter that cannot cancel.

| Executor | Purpose | Advertised features |
| --- | --- | --- |
| `LocalProcessExecutor` | `argv` execution in a Bubblewrap sandbox | `cancel`, `isolation`, `signal`, `suspend` (POSIX) |
| `ShellExecutor` | POSIX shell, gated by explicit `shell` authority | inherits local |
| `CodexExecutor` | Codex CLI over a JSON stream | `cancel`, `streaming` |
| `ClaudeExecutor` | Claude Agent SDK | none — no reliable cancel/checkpoint |
| `DeepSeekExecutor` | DeepSeek API | none — no reliable cancel/checkpoint |
| `RemoteExecutor` | Dispatch to a registered, fenced worker | `streaming` + negotiated controls |
| `FakeExecutor` | Deterministic executor for tests and smoke servers | `cancel` |

The three agent adapters require `isolated_worker=True` — a host assertion that the
worker is independently confined. The flag does not create a sandbox. See
[isolation](docs/isolation.md).

## Serving and consuming the control plane

Host the ASGI application; anonymous access is denied by default:

```sh
PRAXIS_API_TOKEN=$(openssl rand -hex 24) \
uv run --with uvicorn uvicorn examples.server:create_app --factory --host 127.0.0.1 --port 8000
```

Submit, stream and control from the bundled typed client:

```python
from praxis.client import Client
from praxis.client.http import ClientHTTPTransport
from praxis.kernel.spec import ProcessSpec

client = Client(ClientHTTPTransport(base_url, headers={"Authorization": f"Bearer {token}"}))
receipt = await client.submit(ProcessSpec("example", "fake"), idempotency_key="example-1")

cursor = 0
async for stored in client.events(receipt.process_id, after=cursor):
    cursor = stored.cursor  # persist only after the consumer has processed the event
view = await client.inspect(receipt.process_id)
```

The sample server is deliberately one operator and a fake executor. Put TLS, rate
limits, deadlines and a real authorization rule in front of it before exposure —
[operations](docs/operations.md) and [API](docs/api.md) describe what a host owes.

## Documentation

Indexed in [docs/](docs/README.md).

| Document | Read it for |
| --- | --- |
| [Concepts](docs/concepts.md) | The execution model, end to end, with runnable code |
| [Architecture](docs/architecture.md) | Ownership boundaries and failure semantics |
| [Configuration](docs/configuration.md) | TOML keys, precedence, event envelopes |
| [Control-plane API](docs/api.md) | HTTP routes, status codes, SSE cursors |
| [Client SDK](docs/client.md) | Typed submit/inspect/control/stream flows |
| [Operations](docs/operations.md) | Install, serve, upgrade, back up, recover |
| [Threat model](docs/threat-model.md) | TM-1..TM-9 trust boundaries and what is excluded |
| [Isolation](docs/isolation.md) | Sandbox construction, mounts, output limits |
| [Secrets](docs/secrets.md) | Provider integration and injection boundaries |
| [Redaction](docs/redaction.md) | What is scrubbed before bytes leave the controller |
| [Metrics](docs/metrics.md) | Stable metric names, units, label allowlists |
| [Versions](docs/versions.md) | Wire/storage version matrix, compatibility policy |
| [Qualification](docs/qualification.md) | v1.0.0 release evidence and deployment limits |

Start with [concepts](docs/concepts.md); read the [threat model](docs/threat-model.md)
and [isolation](docs/isolation.md) before hosting native workloads.

## Development

```sh
uv run pytest          # 371 tests
uv run ruff check .
uv run mypy            # strict, over src/
```

The reproducible release gate builds an sdist, builds a wheel from it, installs that
wheel into a fresh virtual environment with locked dependencies, verifies imports
resolve to that installation, then runs the suite, lint, types, examples and CLI:

```sh
uv run python scripts/qualify.py
```

CI runs the source checks on Python 3.11–3.14 and the fresh-wheel gate on 3.12.

## Layout

```
src/praxis/
  kernel/          processes, authority, budgets, contracts, effects, graphs, runtime
  executors/       adapter protocol, local/shell/remote, Codex/Claude/DeepSeek, isolation
  workspaces/      private staging, snapshots, canonical transactions
  storage/         SQLite store, journal, migrations, graph store, integrity
  validators/      verification protocol, policy evaluation, command validator
  evaluators/      candidate scoring and winner selection
  api/             ASGI application, control-plane service, auth hooks
  client/          typed v1 HTTP client
  transport/       bounded HTTP transport shared by client and providers
  remote/          worker registration, dispatch, placement, heartbeats, recovery
  knowledge/       context providers, Noesis integration, publication policy
  observability/   metrics, tracing, redaction, structured errors, health
  compatibility.py version matrix and negotiation
  cli.py           console entry point
docs/              reference documentation
examples/          offline demo and sample API server
scripts/           release qualification gate
tests/             suites outside the package, run against the installed distribution
```

## License

MIT. See [LICENSE](LICENSE).
