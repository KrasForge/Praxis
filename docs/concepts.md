# Concepts

This page is the introduction. It walks through the execution model in the order
that the kernel uses, with code you can run. [Architecture](architecture.md)
gives the same model as failure semantics and boundaries of ownership. This page
shows the mechanism.

## The one idea

Two questions have two different answers. Praxis never mixes them.

- **Did the executor finish?** The answer is `ProcessResult.outcome.status`.
- **Did the work succeed?** The answer is `ProcessResult.state`.

A program can exit `0` and still cause a `failed` process, because verification
is independent of execution. All the other parts of the kernel keep that
separation true through a restart, a retry, a remote dispatch, a cancellation
and a recovery.

```python
result = kernel.result(process_id)
result.outcome.status   # OutcomeStatus.COMPLETED  — the executor ran to completion
result.state            # State.FAILED             — verification did not approve it
result.verification.missing_outputs   # ('answer.txt',)
```

Only `State.COMPLETED` means work that succeeded and that Praxis verified. Only a
`workspace.committed` receipt proves canonical publication.

## To submit: ProcessSpec

`ProcessSpec` is the versioned submission document. It does not depend on a
vendor. Its schema is closed: an unknown field causes a rejection, and Praxis
does not ignore it. The kernel makes a snapshot of the spec at `create`. Thus a
later change to the caller's object cannot change what runs.

```python
ProcessSpec(
    objective="write the answer",       # required, human-meaningful
    executor="local",                   # required, must be a registered name
    inputs={"argv": [...]},             # executor-specific
    environment={"LANG": "C"},          # ordinary strings, never secrets
    contract={"required_outputs": ["answer.txt"]},
    budget={"wall_milliseconds": 30_000},
    context=[...],                      # declared context dependencies
    metadata={"example_app": {...}},    # namespaced host/vendor annotations
)
```

A spec carries **no authority**. If a submitted spec contains `capabilities`,
Praxis rejects it. Grants come from the registry of the kernel. They never come
from the document that describes the work. See TM-1 in the
[threat model](threat-model.md).

Praxis counts budgets in exact integer units, over `tokens`, `cost_microusd`,
`wall_milliseconds`, `cpu_milliseconds` and `tool_calls`. Budgets control
accounting and scheduling. They do not contain a process. For limits on CPU,
memory, disk and the count of processes, use the cgroups and the quotas of the
host (TM-9).

The kernel applies two limits at launch:

- If the wall budget is zero, the process does not start.
- If the wall budget is finite, the executor must advertise `cancel`. The kernel
  does not accept a deadline that it cannot apply.

A budget other than `wall_milliseconds` also needs an executor that advertises
`resource_reporting`. Unknown usage is usage that nobody reported. It is never
proof of zero cost.

## To run: executors and workspaces

`Kernel.create` allocates the budget, stores the identity and configures the
authority. `Kernel.start` makes a private workspace and builds an
`ExecutionRequest` that is bound to an attempt. Each attempt gets a new
`attempt_id`. A retry and a fence keep the identity of the process, but they
never use an attempt again.

```python
process = kernel.create(spec, canonical=canonical)
kernel.start(process.process_id)
outcome = await kernel.tasks[process.process_id]
```

The workspace is the only path on the host that the process can write. With the
default `LinuxIsolation`, `LocalProcessExecutor` runs the program in an empty
root. The program gets these controls:

- Separate user, PID, network, IPC and UTS namespaces
- Dropped capabilities
- A private `/proc`, `/dev` and `/tmp`
- Read-only mounts of trusted runtimes
- A writable bind of that one workspace

The `cwd` of a subprocess is not isolation. See [isolation](isolation.md).

An executor advertises its features. It does not imply them.

```python
executor.descriptor.features                             # {'cancel', 'isolation', 'signal', 'suspend'}
executor.descriptor.matches(frozenset({"checkpoint"}))   # False — do not schedule here
```

The vocabulary of features is `checkpoint`, `restore`, `signal`, `isolation`,
`streaming`, `resource_reporting`, `suspend` and `cancel`.

A protocol version that agrees does not imply cancellation, checkpoints or
reports of resources. Ask the descriptor.

## To judge: contracts and verification

When execution ends, the kernel makes a snapshot of the workspace. A snapshot is
immutable bytes with an identity. The kernel then runs the validators against a
**disposable copy** of that snapshot. The contract gives the meaning of
approval.

| Contract field | Effect |
| --- | --- |
| `required_outputs` | Normalized paths, relative to the workspace, that must be in the snapshot |
| `invariants` | Checks that are always required |
| `validators` | Checks that can be required or advisory |
| `acceptance_checks` | Check IDs that become required |
| `quorum` / `quorum_checks` | At least *n* of these checks must pass |

A validator result that is absent is `UNAVAILABLE`. It is not a pass.
`VerificationReport.approved` is true only when there are no required failures,
no missing outputs, and the quorum holds. Validators are trusted policy: a
validator that an attacker controls can approve bad content (TM-4).

## To publish: workspaces, snapshots, canonical directories

`CanonicalDirectory` is a managed store of versions. It is not a Git checkout.
Its `current` pointer moves atomically, and only when all of these are true:

1. Execution succeeded and verification approved the work.
2. The approved snapshot is the snapshot that Praxis commits, and it is not
   stale.
3. The baseline that the work started from is still current.
4. This process holds `Resource.WORKSPACE` authority for the `commit` action.
5. For remote work, the generation fence of the worker is still live.

```python
authority.issue(pid, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
authority.issue(pid, Resource.WORKSPACE, frozenset({"commit"}), pid)
```

A process can complete with no canonical target. That is an ordinary success. It
is not a publication. Do not read one as the other.

## To reach outside: effects

An **effect** is anything that touches the world outside the workspace, for
example a git commit, a message or a publication of an artifact. Praxis stages
an effect and authorizes it apart from execution.

```
proposed → approved → applying → applied
         ↘ rejected            ↘ failed
```

To apply an effect, Praxis needs current authority and a current state of
approval. Model output that *describes* an effect does nothing. An approval is
bound to an attempt and to a version. Thus a stale approval cannot authorize new
work.

An acknowledgement that Praxis lost is the difficult condition. The `applying`
state is **uncertain**. It is never proof that the effect did not occur. Before
you retry, reconcile with the idempotency receipt of the effect. Never repeat a
non-replayable effect blindly. See the section on recovery in
[operations](operations.md).

## To compose: parents, children, graphs

Praxis keeps two relations apart, on purpose:

- A **parent-child** relation controls the lifecycle and the inheritance of a
  budget. A parent in a terminal state cannot make a child.
- A **graph dependency** is a separate edge in a DAG. It has a requirement of
  success or of a terminal state, and a policy of `block`, `fail` or `continue`.
  `GraphStore` keeps these edges, and it keeps the versions of each change.

```python
graph = ProcessGraph(frozenset({first.process_id, second.process_id}),
                     (Dependency(first.process_id, second.process_id),))
GraphStore(store).create(graph, first.process_id)
graph.resolve(node_id, states).state
# 'runnable' | 'blocked' | 'failed' | 'active' | 'finished'
```

There is no global graph daemon. The host finds which nodes are ready and
queues them. `Supervisor` and the services for speculative candidates
coordinate the fork, the join, the evaluation, the selection and the
publication of the winner only.

## Authority

A capability has a type. The kernel issues it. A delegation can only make it
smaller.

| Resource | Actions |
| --- | --- |
| `FILESYSTEM` | `read`, `write` |
| `NETWORK` | `connect`, `listen` |
| `EXECUTOR` | `execute`, `shell`, `control` |
| `WORKSPACE` | `create`, `inspect`, `snapshot`, `commit`, `destroy` |
| `SECRET` | `read` |
| `EFFECT` | `stage`, `apply`, `approve` |

A delegation must make the scope, the actions, the limits on bytes and the
expiration smaller. If you revoke an ancestor, Praxis revokes its descendants. A
serialized capability is a record of a grant. It is not a grant, and to present
one gives no authority. A Python plugin in the process runs with the privileges
of the kernel, so you must trust it. A native workload that an attacker controls
is a different threat from an installed adapter that an attacker controls.

A secret is never a field in a spec. The host binds a name to a provider. Praxis
authorizes the full batch before it resolves any value. Values go only into the
environment of the subprocess. They never go into stored requests, dispatch
envelopes, checkpoints or events. See [secrets](secrets.md).

## To survive: persistence and recovery

SQLite stores the processes, the ordered events, the cursors, the checkpoints
and the records of each subsystem, and it does so atomically. A kernel is a
**single owner** of the controller. The map of tasks in memory is not a
distributed lock.

```python
store = SQLiteStore(path)
kernel = Kernel(store, workspaces, executors, authority=authority)
kernel.recover_records()      # rebuilds processes, results, grants, usage
```

`recover_records` rebuilds the stored record. It does not make the live
subprocesses again, and it retries nothing. Where certainty is not available,
recovery stays manual, on purpose:

- A heartbeat that timed out is **not** confirmation that the work stopped.
  Before you move the work, confirm positively that the old worker stopped.
- Praxis does not rebuild the staged candidate transactions that were in memory.
  The bytes that remain are material for diagnosis. They are not permission to
  publish.
- After a canonical commit, Praxis blocks a replay.

Events use JSON envelopes of version one. `Kernel.metrics.snapshot()` projects
the committed events through a stable cursor. Thus repeated scrapes do not count
an event twice, and a new runtime rebuilds the same metrics from its journal.
See [metrics](metrics.md) and [configuration](configuration.md).

## To distribute: remote workers

A worker registers an identity and a **generation**. It advertises its
capabilities and it renews a heartbeat lease. `DistributedScheduler` reserves
the capacity and sends versioned bundles of the workspace. Praxis fences a
result against the current generation before any canonical commit. Thus a late
result from a stale worker cannot publish. A replay cannot make a second
execution, and it cannot restore a reservation that Praxis released.

Version 1 authenticates a worker, and it trusts that worker to report its
execution and its usage honestly. There is no remote attestation. An operator of
a worker that an attacker controls is outside the model (TM-6). Put a workload
that holds secrets only on a worker that you trust with those secrets.

## Where to go next

- To run it: `uv run python examples/demo.py`
- To host it: [operations](operations.md), [API](api.md), [client](client.md)
- To secure it: [threat model](threat-model.md), [isolation](isolation.md),
  [secrets](secrets.md), [redaction](redaction.md)
- To depend on it: [versions](versions.md), [qualification](qualification.md)
