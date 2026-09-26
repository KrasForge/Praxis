# 0001: Recover staged candidate transactions from the journal

- **Status:** Accepted, 2026-09-26
- **Issue:** #249

## Context

A speculative candidate that passes verification does not commit at once. The
kernel holds its `WorkspaceTransaction` until a selection names a winner. Before
this decision, that transaction and the canonical target of the process existed
only in memory. After a restart:

- `commit_selected` could not find the transaction for a candidate.
- The kernel did not know that the candidate had a canonical target. The
  commit path then released the candidate without a canonical commit.

Nothing false was published, because only `workspace.committed` proves a
publication. But the operator had to reconcile every staged candidate by hand.

## Decision

1. When the kernel holds a verified transaction for selection, it appends
   `transaction.staged`. The event records the workspace, the verified snapshot,
   the canonical root, the baseline revision, the baseline snapshot and the
   attempt.
2. `Kernel.recover_records()` replays the journal. A staged transaction stays
   pending until `workspace.committed`, `workspace.rolled_back`,
   `candidate.released` or `transaction.abandoned` closes it.
3. Recovery rebuilds a pending transaction only when all of these are true: the
   attempt completed; its recovered verification approved that snapshot; the
   canonical directory exists and still points at the baseline revision; the
   retained workspace hashes to the snapshot; and the baseline manifest and blobs
   verify. It then appends `transaction.recovered`.
4. In every other case recovery appends `transaction.abandoned` with a reason.
   Each later recovery reads that event again, so `commit_selected` refuses the
   candidate after every restart, not only the first.
5. `commit()` keeps all of its checks under the canonical lock. A rebuilt
   transaction has no weaker path to publication than one that stayed in memory.

## Consequences

- A restart between staging and selection no longer loses verified work. The
  commit publishes the same snapshot that an uninterrupted run publishes.
- A moved baseline, a changed or missing workspace, or a lost manifest fails
  closed. The work must run again against the current canonical revision.
- Recovery opens a canonical directory that already exists. It never makes one.
- Abandoned workspaces stay on disk until retention removes them.

## Compatibility

The event envelope and the SQLite layout do not change. Event types are open
strings, and the three new types are documented in
[configuration](../configuration.md). A journal written before 1.2.0 has no
`transaction.staged` events, so its staged candidates cannot be rebuilt.
[Operations](../operations.md#staged-candidate-transactions) tells operators to
run those candidates again.

## Verification

`tests/test_transaction_recovery.py` covers these cases:

- A restart between staging and commit.
- A baseline that moved during the outage, including a second restart.
- A tampered workspace, a deleted workspace and a lost baseline manifest.
- A canonical directory that was removed.

## Supersedes

None.
