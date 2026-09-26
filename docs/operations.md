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
[Host](host.md) puts this wiring, the client authentication, TLS and the
publication trigger behind one configuration file.

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

### Staged candidate transactions

A verified candidate that waits for selection has a staged transaction. Praxis
records it in a `transaction.staged` event. `recover_records()` rebuilds the
transaction only when all of these conditions are true:

- The process completed, and verification approved the staged snapshot.
- The canonical directory still exists, and its current revision is still the
  baseline revision. Recovery does not make a missing canonical directory.
- The retained workspace still gives the verified snapshot.
- The baseline manifest and its blobs are still present, for a rollback.

Praxis then records `transaction.recovered`, and `commit_selected` can publish
the candidate. The commit checks the snapshot and the baseline again, under the
canonical lock.

If a condition is false, Praxis records `transaction.abandoned` with one of
these reasons: `process_not_completed`, `verification_unavailable`,
`canonical_missing`, `stale_baseline`, `snapshot_missing`,
`snapshot_mismatch` or `baseline_missing`. `commit_selected` then refuses the
candidate with `staged_transaction_abandoned`, after this restart and after all
later restarts. To publish the work, run the candidate again against the current
canonical revision. The retained bytes are material for diagnosis only.

A journal from a release before 1.2.0 has no `transaction.staged` events.
Praxis cannot rebuild the transactions of those candidates. Do not select a
candidate that was staged before the upgrade: run it again.

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

### Retention

Workspaces, snapshot blobs and canonical revisions grow until you remove them.
The retention sweep removes only the state that no recovery path can still use:

| Item | Praxis removes it when | Praxis keeps it when |
| --- | --- | --- |
| Workspace | Its process has been terminal for `workspace_days` | The process is not terminal or is not in the store; the workspace holds a pending staged transaction; the process has an uncertain effect |
| Snapshot manifest | Nothing references it, and it is older than `workspace_days` | A pending staged transaction or a checkpoint references it |
| Blob | No kept manifest references it, and it is older than `workspace_days` | A kept manifest references it |
| Canonical revision | It is older than the newest `canonical_revisions` superseded revisions | It is current, or it is the baseline of a pending staged transaction |

If a process that is not terminal is older than `workspace_days`, the sweep
keeps every manifest and blob. That process can still roll back to its
baseline. The sweep takes the canonical lock before it removes a revision, and
it takes the snapshot lock before it removes a blob.

Run it on demand. Without `--apply` it only reports:

```sh
praxis retention --db data/runtime.db --workspaces data/workspaces \
    --workspace-days 14 --canonical /srv/canonical --canonical-revisions 5
praxis retention ... --apply
```

`--apply` removes the items and journals a `retention.removed` event on the
process that owned each workspace or revision. It also writes the full report to
`<workspaces>/retention/<milliseconds>.json`. The sweep reads the store, but it
does not recover a kernel. You can run it beside a live controller. The
[host](host.md#retention) can run it on a schedule.

The sweep does not compact the journal. The process records and events are the
audit trail. They are also the idempotency record for submissions, effects and
publication. See [ADR 0002](adr/0002-retention-excludes-the-journal.md). Logs and
backups still need retention policies from the host.
