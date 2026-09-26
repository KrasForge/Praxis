# 0006. Retention removes files, never the journal

- **Status:** Accepted
- **Date:** 2026-09-26
- **Supersedes:** none
- **Issue:** #248

## Context

Workspaces, snapshot manifests, blobs and canonical revisions grow without a
limit. Before this decision, the operator had to remove them by hand. Removing
the wrong item breaks recovery. Examples are the workspace of a staged candidate,
the baseline manifest of a live rollback, and the current canonical revision.

The issue also proposed to compact the journal for processes that were terminal
for a long time. The process records and events are more than a log:

- `POST /v1/processes` finds a replayed `Idempotency-Key` through the
  `submission_key` of `process.created`. If Praxis deleted that event, a replay
  would make a second process.
- Retry and orphan relocation read `effect.applying`, `effect.applied` and
  `workspace.committed` to refuse an unsafe replay.
- The host's publication worker, the effect service and the knowledge
  publication tables refer to process and event identities.
- The journal is the audit trail that the threat model depends on.

## Decision

1. `praxis.workspaces.retention.sweep` removes these items:
   - terminal workspaces
   - unreferenced snapshot manifests and blobs
   - superseded canonical revisions
2. The rules are strict. A workspace stays when its process is not terminal or is
   unknown, when it holds a pending staged transaction (ADR 0005), or when the
   process has an uncertain effect. A manifest stays when a pending staged
   transaction or a checkpoint references it. A blob stays when a kept manifest
   references it. The current revision and the baseline of a pending staged
   transaction stay. If a process that is not terminal is older than the window,
   no manifest or blob is removed.
3. The sweep reads the journal and never rewrites it. Each removal owned by a
   process is appended as `retention.removed`. Each applied sweep writes a report
   under `<workspaces>/retention/`.
4. The default is a dry run. The CLI and the host command need `--apply`.
5. Snapshots share a file lock that the sweep holds exclusively while it removes
   blobs. Revisions are removed under the canonical lock.
6. The journal is not compacted. Its size grows with the count of processes,
   not with their data. If compaction is needed later, it needs its own design,
   with tombstones that keep idempotency.

## Consequences

- Disk use has a bound for the data that grows fastest.
- The journal still grows. The deployment limits say this.
- An abandoned staged workspace is kept until it expires. After that it is
  removed, because its bytes are only material for diagnosis.
- An operator can preview a sweep beside a live controller, because the sweep
  never recovers a kernel. To apply it on a live data directory, use the host's
  scheduled sweep, which runs inside the single controller. A standalone
  `--apply` is for a stopped controller. It re-reads each owner before it
  removes a workspace, but it cannot exclude every race with a live retry.

## Compatibility

No wire or storage change. `retention.removed` is a new event type on the v1
envelope. The host config gains an optional `[retention]` table.

## Verification

`tests/test_retention.py` covers these cases:

- A dry run that changes nothing, and a sweep that removes items and journals
  them.
- Every keep rule.
- Snapshots that checkpoints and staged transactions reference.
- Canonical pruning with a pinned baseline.
- Recovery, and a staged commit, after a sweep.
- The CLI.

`tests/test_host_operations.py` covers the host configuration, the command and
the scheduled sweep.
