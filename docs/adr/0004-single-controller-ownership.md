# 0004. A single controller owns each store

- **Status:** Accepted
- **Date:** 2026-09-26
- **Supersedes:** none

## Context

Praxis already runs with one controller, but no decision records it. The rule
is spread across several documents:

- `docs/architecture.md`: "A kernel is a single owner of the controller. The
  map of tasks in memory is not a distributed lock."
- `docs/host.md`: multi-controller placement, distributed rate limiting, and
  the removal of a client CA without a restart are not covered.
- `docs/qualification.md`: the controller is a single owner, and graph
  readiness and distributed scheduling need orchestration from the host.

Several parts of the code assume one owner. `Authority` holds grants in memory,
and it rebuilds them from the journal only at recovery.
Rate limits live in the memory of the host. The kernel tracks running tasks in
a local map.

## Decision

1. **One controller owns one store.** A controller is a kernel, its host and
   its SQLite store. v1.x does not support two controllers on the same store.
2. **Remote workers scale execution.** They scale the work, not the control
   plane. Placement, fencing and heartbeats stay with the single controller.
3. **Availability comes from restart and recovery.** It does not come from
   replicas. An operator restarts the controller, `Kernel.recover_records`
   rebuilds its state, and backups follow `docs/operations.md`.
4. **A second owner fails closed.** A controller takes an exclusive lock on its
   data directory at startup. A second controller on the same directory
   refuses to start.
5. **Multi-controller support needs a new ADR that supersedes this one.** That
   ADR must cover at least:
   - an ownership lease with a fencing token, checked at every canonical
     commit and every effect application;
   - grants read from the shared store at each decision, not only at recovery;
   - rate limits shared between controllers;
   - graph readiness that more than one controller can resolve safely.

## Consequences

- The limit is written down once, and all three documents can point to it.
- A deployment that starts two controllers on one store by mistake fails at
  startup. It does not risk two conflicting commits later.
- High availability is limited to the time a restart takes.

## Compatibility

- There is no change to the wire format, the storage layout or the
  configuration.
- The data directory gains a lock file. A host from before this change ignores
  it.

## Verification

- A host test: a second host on the same data directory refuses to start, and
  it starts after the first one stops.
- A crash test: a lock left by a process that was killed does not block a
  restart. The lock is held by the process, not by the presence of a file.
