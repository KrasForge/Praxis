# Install and operate

Use Python 3.11 to 3.14. From a clean checkout, install uv and run `uv sync
--locked`. On Linux, install Bubblewrap with the package manager of your
distribution, and permit user namespaces according to the policy of your host.

`uv run python examples/demo.py` tests local execution, verified commit, graphs,
an offline adapter fixture, the Noesis contract, Modulo inspection and the reads
after a restart. CI runs this example. It makes no model calls and it publishes
nothing.

The CLI supplies help and version today. An application embeds a Kernel or hosts
the ASGI factory. For a local smoke server with one operator, supply a strong
`PRAXIS_API_TOKEN` through the secret mechanism of your host, and then run:

```sh
uv run --with uvicorn uvicorn examples.server:create_app --factory --host 127.0.0.1 --port 8000
```

The sample uses a fake executor and a persistent `PRAXIS_DATA` (the default is
`./praxis-data`). `PRAXIS_API_TOKEN` is authentication material for the host. It
is not a `ProcessSpec` environment value.

Before you expose the server on a network, put a reverse proxy in front of it,
with TLS, rate limits and deadlines. For more than one user, replace the sample
rule for authorization. An optional ASGI server and a model SDK are dependencies
of the host. The core package does not install them.

For a real local workload, register `LocalProcessExecutor` and grant the
authority for execution and for the workspace explicitly.

For a live agent, register `CodexExecutor`, `ClaudeExecutor` or
`DeepSeekExecutor` only inside a worker that something else confines, and set
`isolated_worker=True` there. Install the matching optional CLI or SDK, and
supply credentials that are local to that worker.

Codex supports cancellation. The stateless interfaces of Claude and DeepSeek do
not give reliable controls for cancellation or checkpoints. Do not schedule a
workload that depends on cancellation onto an adapter that does not support it.
Read [isolation](isolation.md) and [secrets](secrets.md).

Noesis retrieval uses `HTTPTransport(base_url, headers=host_credentials)` and
`NoesisContextProvider`. Its domain filter maps to
`/api/v1/kb/{domain}/search`. To publish, configure `NoesisExporter` and
`PublicationService` with a policy, an effect capability and a durable store.
Confirm the ingest route of the deployment (the default is
`/documents/ingest`) before you permit a write. Run the offline fixtures first.
Then qualify the true versions, credentials and endpoint contracts of the
provider before you go to production.

## Upgrade and backup

1. Stop new submissions and mutations. Drain the tasks, or terminate them
   positively and record every worker and effect that stays unresolved. Stop the
   single owner of the controller.
2. Back up the SQLite database with `sqlite3.Connection.backup`. Copy the file
   only after a clean shutdown with no live WAL writer. Back up these items
   together:
   - The workspace records, blobs and snapshots
   - The candidate directories that you retain
   - The canonical revisions and the current pointer
   - The worker stores
   - The host configuration

   Exclude the secret values, but keep the references to the provider. Restore
   the credentials separately. A backup is privileged data: restrict it and
   encrypt it.
3. Test the backup in an isolated directory. Verify the SQLite `integrity_check`.
   Inspect the identities of the processes, events and checkpoints. Keep the old
   package and the snapshot available.
4. Install the new locked release. SQLiteStore upgrades a supported layout
   transactionally. Run the tests and the examples. Inspect the health, the
   processes, the effects and the records for recovery before you accept
   submissions again.
5. If a migration fails, the earlier layout stays recoverable. To roll the
   application back, stop the new owner and restore the complete matched backup.
   Never run an older binary against a newer layout. Never combine a database
   with canonical or workspace versions that do not match it.

## Recovery

Make the providers, the authority policy and the executor registry of the host
again. Open SQLiteStore, make a Kernel, and then call `recover_records()`. This
rebuilds the stored processes, results, grants and usage. It does not make the
live subprocesses again, and it does not retry them automatically. Keep the API
writes disabled while you audit the records that are running or suspended.

For a remote assignment, use the detection of OrphanRecovery and its positive
`confirm_stopped` hook before you relocate. A fence and a retry keep the
identity of the process, but they allocate a new attempt ID. A heartbeat that
timed out is not confirmation that the work stopped.

Inspect the effects that are applying or uncertain. Reconcile them with their
idempotency receipts before a retry. Never repeat a non-replayable effect
blindly. Inspect the `workspace.committed` receipts and the history of the
canonical pointer. After a canonical commit, Praxis blocks a replay. Attach the
canonical targets and the scheduler policy of the host deliberately.

Praxis does not rebuild the staged candidate transactions that were in memory.
The bytes that remain are material for diagnosis and recovery. They are not
permission to publish.

A crash of the controller can leave a Noesis publication `in_progress`. An
operator must then reconcile it with the deterministic document ID. Do this
before another decision to publish. There is no daemon that recovers a
publication automatically.

A local orphan process needs the process supervision of the host and positive
confirmation that it stopped. Record the decision that you made in recovery. Do
not infer that a Python task that you lost stopped the native work.

Monitor the health, together with the [metrics](metrics.md), the traces and the
structured errors. A budget unit is an exact integer. Usage that nobody reported
is unknown; it is not proof of zero cost. Keep the quotas for cgroups, processes
and disk outside the kernel. Where a hard wall limit matters, use request
deadlines and an executor that can cancel.

Workspace retention, logs and backups need retention policies from the host.
Version 1 has no daemon for quotas or for garbage collection.
