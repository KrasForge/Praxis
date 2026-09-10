# Praxis

**A process execution kernel that refuses to call unverified work successful.**

Praxis runs work: native programs, shell commands, agent adapters and remote
workers. It owns the rules around that work: authority, verification,
transactional publication and recovery. A process that exits `0` did not
necessarily succeed. Praxis keeps those two facts apart in the types, in the
stored record, and at the commit boundary. Thus a failed or an unverified result
cannot become a canonical artifact.

Praxis is a library and an ASGI service, not an application. Modulo owns the
interface for people. Noesis owns knowledge. Adapters hold the code for each
model; the kernel does not.

- **Status:** v1.0.0, with v1 wire contracts and SQLite layout 2. The
  [qualification report](docs/qualification.md) gives the coverage and the
  deployment limits.
- **Requires:** Python 3.11 to 3.14. Local sandboxed execution needs Linux and
  [Bubblewrap](https://github.com/containers/bubblewrap).
- **Dependencies:** none at runtime. The kernel, the API, the client and the
  adapters use only the standard library. Model SDKs and ASGI servers are host
  dependencies.

## Install

```sh
uv sync --locked          # editable install with dev tooling
uv run python examples/demo.py
```

`examples/demo.py` runs offline. It makes no model calls and it publishes
nothing. It tests local execution, verified commit, graphs, an adapter fixture,
the Noesis contract, control-plane inspection and recovery after a restart.

## Quickstart

Run a program, require an artifact, and publish the artifact only if
verification approves it.

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

The process ran in a private sandboxed workspace. Praxis made a snapshot of the
output and checked it against the contract. Only then did Praxis commit it to
the canonical directory.

### The invariant, concretely

Now replace the program with one that writes nothing: `"pass"`. Change nothing
else. The program still exits `0`, so the executor still reports `completed`.

```
failed completed False
```

`result.verification.missing_outputs` is `('answer.txt',)`, and the canonical
directory is empty. The executor completed, but the process failed, because
verification did not approve it. Thus Praxis published nothing. This difference
between `state` and `outcome.status` is the core of the kernel. It holds after a
restart, a retry, a remote dispatch and a recovery.

## Core model

| Concept | What it is |
| --- | --- |
| `ProcessSpec` | The versioned submission: objective, executor, inputs, environment, contract, budget, context and metadata. The schema is closed, thus an unknown field causes a rejection. |
| `Outcome` | What the executor did: `completed`, `failed`, `cancelled`, `timed_out`, `partial`, `budget_exhausted` or `unavailable`. |
| `ProcessResult` | What the kernel concluded: the terminal `state`, the outcome, an independent `verification`, artifacts, evidence, claims, effects and usage. |
| `Contract` | The acceptance rules that a machine can check: required outputs, invariants, validators and quorum. |
| Workspace | A private directory for staging. A snapshot identifies immutable bytes. Validators use disposable copies. |
| `CanonicalDirectory` | A managed store of versions. Its `current` pointer moves atomically, and only after the checks of verification, baseline and authority. |
| `Capability` | Authority with a type, issued by the kernel, over filesystem, network, executor, workspace, secret or effect scopes. A delegation can only make a scope smaller. |
| Effect | A write to the world outside the workspace, for example a git commit, a message or a publication. Praxis stages it and approves it apart from execution, and gives it an idempotency receipt. |
| `ProcessGraph` | Dependencies in a DAG, with requirements for success or for a terminal state, and policies of block, fail or continue. `GraphStore` keeps them. |

Read [concepts](docs/concepts.md) to see how these parts fit together. Read
[architecture](docs/architecture.md) for the failure semantics.

## Executors

Register a map of names to executors with the kernel. An executor advertises its
features; the kernel does not assume them. A protocol version that agrees does
not show support for cancellation or for checkpoints. If an adapter cannot
cancel, the kernel refuses work that has a wall limit.

| Executor | Purpose | Advertised features |
| --- | --- | --- |
| `LocalProcessExecutor` | Runs `argv` in a Bubblewrap sandbox | `cancel`, `isolation`, `signal`, `suspend` (POSIX) |
| `ShellExecutor` | Runs a POSIX shell, after a check of explicit `shell` authority | the same as local |
| `CodexExecutor` | Uses the Codex CLI through a JSON stream | `cancel`, `streaming` |
| `ClaudeExecutor` | Uses the Claude Agent SDK | none: no reliable cancel or checkpoint |
| `DeepSeekExecutor` | Uses the DeepSeek API | none: no reliable cancel or checkpoint |
| `RemoteExecutor` | Sends work to a registered worker that has a fence | `streaming` and the negotiated controls |
| `FakeExecutor` | Gives deterministic results for tests and smoke servers | `cancel` |

The three agent adapters need `isolated_worker=True`. This flag is an assertion
by the host that something else confines the worker. The flag does not make a
sandbox. See [isolation](docs/isolation.md).

## Serving and consuming the control plane

Host the ASGI application. By default it denies anonymous access.

```sh
PRAXIS_API_TOKEN=$(openssl rand -hex 24) \
uv run --with uvicorn uvicorn examples.server:create_app --factory --host 127.0.0.1 --port 8000
```

Submit work, stream events and control the process with the typed client:

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

The sample server has one operator and a fake executor, on purpose. Before you
expose it, put TLS, rate limits, deadlines and a real authorization rule in
front of it. [Operations](docs/operations.md) and [API](docs/api.md) list what a
host must supply.

## Documentation

The index is in [docs/](docs/README.md).

| Document | Read it for |
| --- | --- |
| [Concepts](docs/concepts.md) | The execution model, from end to end, with code you can run |
| [Architecture](docs/architecture.md) | The boundaries of ownership and the failure semantics |
| [Configuration](docs/configuration.md) | The TOML keys, the precedence and the event envelopes |
| [Control-plane API](docs/api.md) | The HTTP routes, the status codes and the SSE cursors |
| [Client SDK](docs/client.md) | The typed flows to submit, inspect, control and stream |
| [Operations](docs/operations.md) | How to install, serve, upgrade, back up and recover |
| [Threat model](docs/threat-model.md) | The trust boundaries TM-1 to TM-9, and what they exclude |
| [Isolation](docs/isolation.md) | The sandbox, the mounts and the output limits |
| [Secrets](docs/secrets.md) | How to integrate a provider, and the limits of injection |
| [Redaction](docs/redaction.md) | What Praxis removes before bytes leave the controller |
| [Metrics](docs/metrics.md) | The stable metric names, units and label allowlists |
| [Versions](docs/versions.md) | The version matrix for the wire and the storage, and the policy |
| [Qualification](docs/qualification.md) | The v1.0.0 release evidence and the deployment limits |

Start with [concepts](docs/concepts.md). Before you host native workloads, read
the [threat model](docs/threat-model.md) and [isolation](docs/isolation.md).

## Development

```sh
uv run pytest          # 371 tests
uv run ruff check .
uv run mypy            # strict, over src/
```

The release gate is reproducible. It builds an sdist and then builds a wheel
from it. It installs that wheel into a new virtual environment with locked
dependencies. Then it makes sure that the imports come from that installation.
Then it runs the test suite, the lint, the type checks, the examples and the
CLI.

```sh
uv run python scripts/qualify.py
```

CI runs the source checks on Python 3.11 to 3.14. It runs the fresh-wheel gate
on 3.12.

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
