# v1.0.0 release qualification

Praxis qualified this release on 2026-09-10. Package 1.0.0 uses the v1 wire
contracts and SQLite layout 2.

The reproducible gate builds an sdist and then builds its wheel. It installs
that wheel into a new virtual environment, with the locked test dependencies. It
then makes sure that the imports come from that installation, and that the
typing metadata ships. Then it runs the full test suite, the lint, the type
checks, the examples and the CLI.

Run it from a clean checkout, with Python 3.11 to 3.14, with uv, and with Linux
and Bubblewrap isolation that works:

```sh
uv sync --locked
uv run python scripts/qualify.py
```

Local qualification: 371 tests passed. Ruff passed. mypy passed across 96 source
files. All six example checks passed. The CLI reported praxis 1.0.0.

CI runs the source tests and examples on Python 3.11, 3.12, 3.13 and 3.14. It
also runs the fresh-wheel qualification on 3.12. These checks are the gate for a
merge. They are not an assertion that a live provider is certified.

## The release checklist

You can reproduce this checklist from a clean checkout. Steps 1 to 6 are
automated and they must pass without a change. In steps 7 to 9 the owner of the
release records a judgement.

1. Clone the release commit into an empty directory. Do not use an existing
   `.venv`, `dist/` or cache again.
2. Install Python 3.11 to 3.14, [uv](https://docs.astral.sh/uv/) and Bubblewrap.
   Permit user namespaces according to the policy of your host. If isolation
   does not work, local execution fails; it does not fall back to access on the
   host.
3. Run `uv sync --locked`. If the lockfile resolves differently, that is a
   blocker for the release. It is not a warning.
4. Run `uv run python scripts/qualify.py`. This builds the sdist, builds the
   wheel from it, and installs that wheel into a new virtual environment with
   the locked test dependencies. It then asserts that `praxis` imports from that
   installation, and that `py.typed` ships. Then it runs the suite, Ruff, mypy,
   the offline examples and the console script.
5. Confirm that the version agrees across `pyproject.toml`, `praxis.__version__`,
   `praxis --version` and the heading of this document.
6. Confirm that CI is green for the release commit, across all four Python
   versions, and that this includes the fresh-wheel job.
7. Read **The known deployment limits** below again. Confirm that every item
   still holds, or change it. A limit that nobody documented is a blocker for
   the release.
8. Confirm that the compatibility statements in [versions](versions.md) still
   agree with the schemas, protocol versions and SQLite layout that this release
   ships. Confirm that no breaking change shipped without a bump of the version
   and a migration.
9. Record the results with the release commit: the output of the qualify script,
   the CI run, and any acceptance of a provider that you did separately. Keep
   this report with them.

A failure at any automated step blocks the release. Do not qualify a checkout
that you modified, and do not qualify a gate that you ran only in part.

| Area | Qualification evidence |
| --- | --- |
| Lifecycle and controls | The runtime, lifecycle, controls, joins, supervisor and retry suites |
| Verification and canonical publication | The verified_execution, transaction, rollback, validators and selection suites, and the verified artifact in the demo |
| Recovery and persistence | The checkpoints, durable_ordering, migrations and orphan_recovery suites, and the reopened database in the demo |
| Graphs | The graph, graph_resolution and graph_store suites, and the stored dependencies in the demo |
| Effects and approvals | The effects, effect_service, approvals, effect_replay and publication suites |
| Adapters | The all_executors conformance suite, real local and shell processes, and the offline protocol fixtures for Codex, Claude and DeepSeek |
| Remote execution | Real worker and controller RPC, native workload tests, reconnection, cancellation, placement, fencing, and adversarial lineage fixtures |
| API/client | The authenticated API, SSE resumption, controls, the client-ASGI integration, and real local tests of the HTTP and SSE wire |
| Security | Native isolation of host, sibling, symlink and network; mutation and escalation of authority; the canaries for secrets and redaction; and 2,000 seeded mutations of the parser |
| Compatibility | The reader and writer matrix across nine boundaries, historical optional fields, negotiated workers, and the transactional rollback from layout 1 to layout 2 |

Execution that failed, and verification that Praxis rejected, cannot make a
verified result that succeeded. Canonical publication also needs a matching
immutable snapshot, a current baseline, authority, and a current remote
generation. The tests reject a stale snapshot, a candidate that failed or that
nobody verified, a changed request of the policy, and an unsafe replay.

A completed ordinary process that has no canonical target is not a committed
artifact. Only `workspace.committed` proves that action.

Qualification made three operational boundaries stricter. A zero budget stops a
launch. Work with a wall limit needs an executor that advertises cancellation.
Praxis bounds the native and Codex output before it accumulates a result. The
restoration of a workspace bundle now applies the permissions of a directory
after it writes the children. These regressions run in the full gate.

## The known deployment limits

- Qualification contacted no live deployment of Codex, Claude, DeepSeek, Noesis
  or Modulo. These items all need acceptance in your deployment: the optional
  versions of an SDK or API; the credentials; the confinement of a worker; the
  networking of a provider; and the real external effects. The fixtures qualify
  the semantics of mapping and errors. They do not qualify the availability of
  an external service.
- Local isolation is Linux and Bubblewrap, with an explicit allowlist of runtime
  images. Another platform needs a worker that you qualified independently. The
  cgroups and quotas of your host give the containment for CPU, memory, PIDs and
  disk. A third-party SDK adapter needs a trusted configuration for an isolated
  worker; the flag alone makes no confinement.
- The controller is a single owner. The supervision of your host owns the
  deadlines for startup and shutdown, and the termination of a local orphan. A
  finite wall budget rejects an adapter that cannot cancel. A checkpoint or a
  restore that Praxis does not support stays explicit. The overhead of a native
  launch, and the startup of a provider, still need deadlines from the host.
- CanonicalDirectory is a managed store of versions. It is not a general
  transaction manager for Git. Praxis does not rebuild the staged candidate
  transaction objects after a restart. The readiness of a graph, and the
  scheduling of a distributed API, need orchestration from the host.
- These conditions need reconciliation: an uncertain effect that you cannot
  replay; remote work with no fence; a canonical revision that Praxis committed;
  and a publication of knowledge that is in progress. No timeout alone
  authorizes a replay. See the runbook for recovery.
- A workload that holds a secret can encode it deliberately, or write it into an
  artifact. Redaction does not replace least privilege, or a policy for the
  network and for publication. The internal journal and the backups contain
  privileged data about processes and capabilities. Configure the capabilities
  of your operators before you attach the kernel journal. A runtime capability
  event needs the lineage of the process.
- Praxis has no built-in policy for the ownership of multiple tenants. It has no
  daemon for the retention of storage, no distributed consensus, no remote
  attestation, and no dependency on a production ASGI server. The sample HTTP
  server uses one operator and a fake executor.

No automated qualification gate fails in the configuration that Praxis tested.
Keep this report with the release commit and the CI checks. The hosting
application must address the deployment limits before it permits the production
integrations that they affect.
