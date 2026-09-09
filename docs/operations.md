# Install and operate

Use Python 3.11–3.14. From a clean checkout, install uv and run `uv sync --locked`. On Linux install Bubblewrap with the distribution package manager and permit user namespaces according to host policy. `uv run python examples/demo.py` exercises local execution, verified commit, graphs, an offline agent fixture, Noesis contract mapping, Modulo inspection and restart reads. CI runs this example. It makes no model calls or external publication.

The CLI currently supplies help/version. Applications embed Kernel or host the ASGI factory. For a local one-operator smoke server, provision a strong PRAXIS_API_TOKEN through your host secret mechanism, then run:

```sh
uv run --with uvicorn uvicorn examples.server:create_app --factory --host 127.0.0.1 --port 8000
```

The sample uses a fake executor and persistent PRAXIS_DATA (default ./praxis-data). PRAXIS_API_TOKEN is host authentication material, not ProcessSpec environment. Use a reverse proxy with TLS, rate limits and deadlines before network exposure, and replace the sample authorization rule for multiple users. Optional ASGI servers and model SDKs are host dependencies, not installed by the core package.

For real local workloads register LocalProcessExecutor and grant execute/workspace authority explicitly. For live agents register CodexExecutor, ClaudeExecutor or DeepSeekExecutor only inside an independently confined worker and set isolated_worker=True there. Install the matching optional CLI/SDK and provision worker-local credentials. Codex supports cancellation; stateless Claude/DeepSeek interfaces do not provide reliable cancellation/checkpoint controls. Do not schedule cancellability-dependent workloads on unsupported adapters. Read [isolation](isolation.md) and [secrets](secrets.md).

Noesis retrieval uses HTTPTransport(base_url, headers=host_credentials) and NoesisContextProvider. Its domain filter maps to /api/v1/kb/{domain}/search. For publication configure NoesisExporter and PublicationService with a policy, effect capability and durable store. Confirm the deployment's ingest route (default /documents/ingest) before enabling writes. Run offline fixtures first and explicitly qualify actual provider versions, credentials and endpoint contracts before production.

## Upgrade and backup

1. Stop accepting submissions and mutations. Drain tasks or positively terminate and record unresolved workers/effects. Stop the single controller owner.
2. Back up the SQLite database using sqlite3.Connection.backup (or copy only after clean shutdown with no live WAL writer). Back up workspace records/blobs/snapshots, retained candidate directories, canonical revisions/current pointer, worker stores, and host configuration together. Exclude secret values; preserve provider references and restore credentials separately. Restrict/encrypt backups as privileged data.
3. Test the backup in an isolated directory, verify SQLite integrity_check, and inspect process/event/checkpoint identities. Keep the old package and snapshot available.
4. Install the new locked release. SQLiteStore upgrades supported layouts transactionally. Run tests/examples and inspect health, processes, effects and recovery records before resuming submissions.
5. On failed migration, prior layout remains recoverable. On application rollback, stop the new owner and restore the complete matched backup; never run an older binary against a newer layout or combine a database with unrelated canonical/workspace versions.

## Recovery

Recreate the host's providers, authority policy and executor registry, open SQLiteStore, instantiate Kernel, then call recover_records(). This reconstructs persisted processes/results/grants/usage; it does not recreate live subprocesses or automatically retry them. Keep API writes disabled while auditing running/suspended records. For remote assignments use OrphanRecovery detection and its positive confirm_stopped hook before relocate. Fencing and retries retain process identity but allocate a new attempt ID. A heartbeat timeout is not termination confirmation.

Inspect applying/uncertain effects and reconcile using their idempotency receipts before retry. Never blindly repeat a non-replayable effect. Inspect workspace.committed receipts and canonical pointer history; replay after a canonical commit is blocked. Reattach canonical targets and host scheduler policy deliberately. In-memory staged candidate transactions are not reconstructed after restart; retained bytes are diagnostic/recovery material, not permission to publish.

A Noesis publication left in_progress by a controller crash requires operator reconciliation with the deterministic document ID before another publication decision. There is no automatic publication recovery daemon. Local orphan processes likewise need host process supervision and termination confirmation; record recovery decisions rather than inferring that a lost Python task stopped native work.

Monitor health plus [metrics](metrics.md), traces and structured errors. Budget units are exact integers; unknown usage is unreported, not zero-cost proof. Keep cgroup/process/disk quotas outside the kernel. Use request deadlines and cancellation-capable executors where hard wall limits matter. Workspace retention, logs and backups require host retention policies; v1 has no automatic quota/garbage-collection daemon.
