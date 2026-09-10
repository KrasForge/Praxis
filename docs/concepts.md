# Concepts

This is the on-ramp. It walks the execution model in the order the kernel walks it,
with code you can run. [Architecture](architecture.md) states the same model as
failure semantics and ownership boundaries; this page shows the mechanism.

## The one idea

Two questions have two different answers, and Praxis never conflates them:

- **Did the executor finish?** — `ProcessResult.outcome.status`
- **Did the work succeed?** — `ProcessResult.state`

A program can exit `0` and still produce a `failed` process, because verification is
independent of execution. Everything else in the kernel exists to keep that
separation true under restart, retry, remote dispatch, cancellation and recovery.

```python
result = kernel.result(process_id)
result.outcome.status   # OutcomeStatus.COMPLETED  — the executor ran to completion
result.state            # State.FAILED             — verification did not approve it
result.verification.missing_outputs   # ('answer.txt',)
```

Only `State.COMPLETED` means *successful, verified work*, and only a
`workspace.committed` receipt proves canonical publication.

## Submitting: ProcessSpec

`ProcessSpec` is the versioned, vendor-independent submission document. Its schema is
closed — unknown fields are rejected rather than ignored — and it is snapshotted at
`create` so later mutation of the caller's object cannot change what runs.

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

A spec carries **no authority**. `capabilities` on a submitted spec is rejected
outright — grants come from the kernel's registry, never from the document
describing the work. See TM-1 in the [threat model](threat-model.md).

Budgets are accounted in exact integer units over `tokens`, `cost_microusd`,
`wall_milliseconds`, `cpu_milliseconds` and `tool_calls`. They are accounting and
scheduling controls, not containment — CPU, memory, disk and process-count limits
need host cgroups and quotas (TM-9). Two boundaries are enforced at launch: a zero
wall budget refuses to start, and a finite wall budget is refused on an executor that
does not advertise `cancel`, because the kernel will not accept a deadline it cannot
enforce. Anything beyond `wall_milliseconds` additionally requires an executor that
advertises `resource_reporting`. Unknown usage is unreported, never proof of zero cost.

## Running: executors and workspaces

`Kernel.create` allocates budget, persists identity and configures authority.
`Kernel.start` creates a private workspace and builds an attempt-bound
`ExecutionRequest`. Each attempt gets a fresh `attempt_id`; retries and fencing keep
process identity but never reuse an attempt.

```python
process = kernel.create(spec, canonical=canonical)
kernel.start(process.process_id)
outcome = await kernel.tasks[process.process_id]
```

The workspace is the only host path the process can write. With the default
`LinuxIsolation`, `LocalProcessExecutor` runs the program in an empty root with
separate user, PID, network, IPC and UTS namespaces, dropped capabilities, private
`/proc`, `/dev` and `/tmp`, read-only trusted runtime mounts, and a writable bind of
that one workspace. A subprocess `cwd` is not isolation; see [isolation](isolation.md).

Executors advertise features rather than implying them:

```python
executor.descriptor.features                             # {'cancel', 'isolation', 'signal', 'suspend'}
executor.descriptor.matches(frozenset({"checkpoint"}))   # False — do not schedule here
```

The advertised vocabulary is `checkpoint`, `restore`, `signal`, `isolation`,
`streaming`, `resource_reporting`, `suspend` and `cancel`.

A matching protocol version does not imply cancellation, checkpointing or resource
reporting. Ask the descriptor.

## Judging: contracts and verification

When execution ends, the kernel snapshots the workspace — immutable bytes with an
identity — and runs validators against a **disposable copy** of that snapshot. The
contract decides what approval means:

| Contract field | Effect |
| --- | --- |
| `required_outputs` | Normalized workspace-relative paths that must exist in the snapshot |
| `invariants` | Always-required checks |
| `validators` | Checks that may be required or advisory |
| `acceptance_checks` | Check IDs promoted to required |
| `quorum` / `quorum_checks` | At least *n* of these checks must pass |

A missing validator result is `UNAVAILABLE`, not a pass. `VerificationReport.approved`
is true only when there are no required failures, no missing outputs, and quorum
holds. Validators are trusted policy: a compromised validator can approve bad
content (TM-4).

## Publishing: workspaces, snapshots, canonical directories

`CanonicalDirectory` is a managed version store, not a Git checkout. Its `current`
pointer moves atomically, and only when all of these hold:

1. Execution succeeded and verification approved,
2. the approved snapshot is the one being committed and is not stale,
3. the baseline the work started from is still current,
4. `Resource.WORKSPACE` / `commit` authority is held for this process,
5. for remote work, the worker's generation fence is still live.

```python
authority.issue(pid, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
authority.issue(pid, Resource.WORKSPACE, frozenset({"commit"}), pid)
```

A process can complete with no canonical target at all. That is an ordinary success,
not a publication — do not read one as the other.

## Reaching outside: effects

Anything that touches the world beyond the workspace — a git commit, a message, an
artifact publication — is an **effect**, staged and authorized separately from
execution:

```
proposed → approved → applying → applied
         ↘ rejected            ↘ failed
```

Applying requires current authority plus current approval state. Receiving model
output that *describes* an effect performs nothing. Approvals are bound to an
attempt and a version, so a stale approval cannot authorize new work.

Lost acknowledgements are the hard case: `applying` is **uncertain**, never proof of
non-execution. Reconcile with the effect's idempotency receipt before any retry, and
never blindly repeat a non-replayable effect. See the recovery section of
[operations](operations.md).

## Composing: parents, children, graphs

Two different relationships, deliberately not merged:

- **Parent/child** controls lifecycle and budget inheritance. A terminal parent
  cannot spawn.
- **Graph dependencies** are separate DAG edges with success or terminal
  requirements and `block` / `fail` / `continue` policies, persisted through
  `GraphStore` with versioned mutations.

```python
graph = ProcessGraph(frozenset({first.process_id, second.process_id}),
                     (Dependency(first.process_id, second.process_id),))
GraphStore(store).create(graph, first.process_id)
graph.resolve(node_id, states).state
# 'runnable' | 'blocked' | 'failed' | 'active' | 'finished'
```

There is no implicit global graph daemon. Hosts resolve readiness and enqueue
runnable nodes. `Supervisor` and the speculative candidate services coordinate
fork/join, evaluation, selection and winner-only publication.

## Authority

Capabilities are typed, kernel-issued and narrowing-only:

| Resource | Actions |
| --- | --- |
| `FILESYSTEM` | `read`, `write` |
| `NETWORK` | `connect`, `listen` |
| `EXECUTOR` | `execute`, `shell`, `control` |
| `WORKSPACE` | `create`, `inspect`, `snapshot`, `commit`, `destroy` |
| `SECRET` | `read` |
| `EFFECT` | `stage`, `apply`, `approve` |

Delegation must narrow scope, actions, byte constraints and expiration. Revoking an
ancestor revokes its descendants. A serialized capability is a record of a grant, not
a grant — presenting one does not confer authority. In-process Python plugins run
with kernel privileges and must be trusted; a malicious *native workload* is a
different threat from a malicious *installed adapter*.

Secrets are never spec fields. Hosts bind names to a provider, the whole batch is
authorized before any value resolves, and values enter only the subprocess
environment — never stored requests, dispatch envelopes, checkpoints or events. See
[secrets](secrets.md).

## Surviving: persistence and recovery

SQLite atomically stores processes, ordered events, cursors, checkpoints and
subsystem records. A kernel is a **single controller owner**; the in-memory task map
is not a distributed lock.

```python
store = SQLiteStore(path)
kernel = Kernel(store, workspaces, executors, authority=authority)
kernel.recover_records()      # rebuilds processes, results, grants, usage
```

`recover_records` reconstructs the persisted record. It does not recreate live
subprocesses and does not retry anything. Recovery is deliberately manual where
certainty is unavailable:

- A heartbeat timeout is **not** termination confirmation. Positively confirm the old
  worker stopped before relocating its work.
- Staged candidate transactions held in memory are not reconstructed after restart.
  Retained bytes are diagnostic material, not permission to publish.
- Replay after a canonical commit is blocked.

Events use version-1 JSON envelopes; `Kernel.metrics.snapshot()` projects committed
events through a stable cursor, so repeated scrapes do not double-count and a new
runtime rebuilds the same metrics from its journal. See [metrics](metrics.md) and
[configuration](configuration.md).

## Distributing: remote workers

Workers register an identity and **generation**, advertise capabilities and renew
heartbeat leases. `DistributedScheduler` reserves capacity and dispatches versioned
workspace bundles. Results are fenced against the current generation before any
canonical commit, so a stale worker's late result cannot publish. Replays cannot
create a second execution or resurrect a released reservation.

v1 authenticates workers and trusts them to report execution and usage honestly.
There is no remote attestation, and a compromised worker operator is outside the
model (TM-6). Secret-bearing workloads belong only on a worker trusted for those
secrets.

## Where to go next

- Run it: `uv run python examples/demo.py`
- Host it: [operations](operations.md), [API](api.md), [client](client.md)
- Secure it: [threat model](threat-model.md), [isolation](isolation.md),
  [secrets](secrets.md), [redaction](redaction.md)
- Depend on it: [versions](versions.md), [qualification](qualification.md)
