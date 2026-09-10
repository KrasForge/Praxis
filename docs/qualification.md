# v1.0.0 release qualification

Qualified on 2026-09-09. Package 1.0.0 uses v1 wire contracts and SQLite layout 2. The reproducible gate builds an sdist, builds its wheel, installs that wheel into a fresh virtual environment with locked test dependencies, verifies imports come from that installation and that typing metadata ships, then runs the full suite, lint, type checks, examples and CLI. Run from a clean checkout with Python 3.11–3.14, uv and working Linux/Bubblewrap isolation:

```sh
uv sync --locked
uv run python scripts/qualify.py
```

Local qualification: 371 tests passed, Ruff passed, mypy passed across 96 source files, all six example checks passed, CLI reported praxis 1.0.0. CI runs source tests/examples on Python 3.11, 3.12, 3.13 and 3.14, plus fresh-wheel qualification on 3.12. These checks are the merge gate, not an assertion of live provider certification.

## Release checklist

Reproducible from a clean checkout. Steps 1–6 are automated and must pass unmodified;
steps 7–9 are judgement the release owner records.

1. Clone the release commit into an empty directory. Do not reuse an existing `.venv`, `dist/`, or cache.
2. Install Python 3.11–3.14, [uv](https://docs.astral.sh/uv/) and Bubblewrap, and permit user namespaces per host policy. Without working isolation, local execution fails closed rather than falling back to host access.
3. `uv sync --locked` — a lockfile that resolves differently is a release blocker, not a warning.
4. `uv run python scripts/qualify.py` — builds the sdist, builds the wheel from it, installs that wheel into a fresh virtual environment with locked test dependencies, asserts `praxis` imports from that installation and that `py.typed` ships, then runs the suite, Ruff, mypy, the offline examples and the console script.
5. Confirm the reported version matches `pyproject.toml`, `praxis.__version__`, `praxis --version` and this document's heading.
6. Confirm CI is green for the release commit across all four Python versions, including the fresh-wheel job.
7. Re-read **Known deployment limits** below and confirm every item still holds, or amend it. An undocumented limit is a release blocker.
8. Confirm the compatibility statements in [versions](versions.md) still match the shipped schemas, protocol versions and SQLite layout, and that no breaking change shipped without a version bump and migration.
9. Record the results with the release commit: the qualify output, the CI run, and any deployment-specific provider acceptance performed separately. Preserve this report alongside them.

A failure at any automated step blocks the release; do not qualify a modified checkout or a partially rerun gate.

| Area | Qualification evidence |
| --- | --- |
| Lifecycle and controls | runtime, lifecycle, controls, joins, supervisor and retry suites |
| Verification and canonical publication | verified_execution, transaction, rollback, validators and selection suites; demo verified artifact |
| Recovery and persistence | checkpoints, durable_ordering, migrations and orphan_recovery suites; reopened database demo |
| Graphs | graph, graph_resolution and graph_store suites; persisted dependency demo |
| Effects and approvals | effects, effect_service, approvals, effect_replay and publication suites |
| Adapters | all_executors conformance; real local/shell processes; Codex/Claude/DeepSeek offline protocol fixtures |
| Remote execution | actual worker/controller RPC and native workload tests, reconnect/cancellation/placement/fencing, adversarial lineage fixtures |
| API/client | authenticated API, SSE resume, controls, client-ASGI integration and real local HTTP/SSE wire tests |
| Security | native host/sibling/symlink/network isolation, authority mutation/escalation, secret/redaction canaries, 2,000 seeded parser mutations |
| Compatibility | nine-boundary reader/writer matrix, historical optional fields, negotiated workers, transactional layout 1-to-2 rollback |

Failed execution or rejected verification cannot create a successful verified result. Canonical publication additionally requires a matching immutable snapshot, current baseline, authority, and a current remote generation. Tests reject stale snapshots, failed/unverified candidates, changed policy requests and unsafe replays. An ordinary completed process without a canonical target is not represented as a committed artifact: only workspace.committed records prove that action.

Qualification tightened three operational boundaries: zero budget prevents launch; wall-limited work requires advertised cancellation; native/Codex output is bounded before result accumulation. Workspace bundle restoration now applies directory permissions after writing children. These regressions run in the full gate.

## Known deployment limits

- Live Codex, Claude, DeepSeek, Noesis and Modulo deployments were not contacted during qualification. Optional SDK/API versions, credentials, worker confinement, provider networking and real external effects require deployment-specific acceptance. Fixtures qualify mapping/error semantics, not external service availability.
- Local isolation is Linux/Bubblewrap and an explicit runtime image allowlist. Other platforms need independently qualified workers. Host cgroups/quotas provide CPU, memory, PID and disk containment. Third-party SDK adapters require trusted isolated-worker configuration; the flag alone does not create confinement.
- The controller is a single owner. Host supervision owns startup/shutdown deadlines and local orphan termination. Finite wall budgets reject adapters without cancellation; unsupported checkpoints/restores remain explicit. Native launch overhead and provider startup still require host deadlines.
- CanonicalDirectory is a managed version store, not a general Git transaction manager. Staged candidate transaction objects are not reconstructed after restart. Graph readiness and distributed API scheduling require host orchestration.
- Uncertain non-replayable effects, unfenced remote work, committed canonical revisions and in-progress knowledge publication require reconciliation; no timeout alone authorizes replay. See the recovery runbook.
- Secrets granted to a workload can be deliberately encoded or written into artifacts; redaction does not substitute for least privilege and network/publication policy. The internal journal/backups contain privileged process/capability data. Configure operator capabilities before attaching the kernel journal; runtime capability events require process lineage.
- There is no built-in multi-tenant ownership policy, storage retention daemon, distributed consensus, remote attestation, or production ASGI server dependency. The sample HTTP server uses one operator and a fake executor.

No failing automated qualification gate remains in the tested configuration. Preserve this report with the release commit and CI checks. Deployment limits must be addressed by the hosting application before enabling the affected production integrations.
