# Architecture and failure semantics

For an introduction to this model, read [concepts](concepts.md) first. This page
gives the boundaries and the semantics precisely.

## Ownership

Praxis owns execution, authority, verification and recovery. Modulo owns the
interaction with people. Noesis owns knowledge. The kernel depends on the
protocols for executors, workspaces and providers. It never depends on the
result semantics of a model. The options of a native SDK stay in the namespace
of its adapter.

## Processes, outcomes and results

A ProcessSpec declares the objective, the executor, the inputs, the ordinary
environment, the acceptance contract, the budget, the context dependencies and
the metadata for scheduling. `Kernel.create` makes a snapshot of the spec,
allocates the budget and stores the identity. `start` makes a workspace and an
executor request that is bound to an attempt.

An Outcome describes the execution. A ProcessResult describes the terminal
state, the independent verification, the effects, the evidence and the usage. A
completed result needs execution that succeeded and verification that Praxis
approved. Thus validation that failed can give `state=failed` together with
`outcome.status=completed`.

Praxis keeps these conditions distinct: cancellation, unavailability, timeout,
partial output, an exhausted budget, a failure of transport and a denial by
policy.

## Workspaces, verification and publication

A workspace is a private directory for staging. A snapshot identifies immutable
bytes. A validator uses a disposable copy of a snapshot.

CanonicalDirectory is a managed store of versions. Its current pointer changes
atomically, after the checks of verification and baseline. It is not a Git
checkout. It does not commit a repository automatically, and it does not push
one. A git commit and any other external write is a staged effect, with its own
authority, approval and reconciliation.

A process can complete without a canonical target. Only a `workspace.committed`
receipt proves canonical publication.

## Composition

A parent-child relation controls the lifecycle and the inheritance of a budget.
A graph dependency is a separate edge in a DAG. It carries a requirement of
success or of a terminal state, and a policy of block, fail or continue.
GraphStore keeps the versions of each change.

The host finds which graph nodes are ready and queues the runnable ones. There
is no global graph daemon. Supervisor and the services for speculative
candidates coordinate the fork, the join, the evaluation, the selection and the
publication of the winner only.

## Persistence and distribution

SQLite stores the processes, the event cursors, the checkpoints and the records
of each subsystem, and it does so atomically. A kernel is a single owner of the
controller. The map of tasks in memory is not a distributed lock.

A worker registers its identity and its generation, advertises its capabilities
and renews a heartbeat lease. DistributedScheduler reserves the capacity and
sends versioned bundles of the workspace. It fences a stale result before a
canonical commit.

An acknowledgement that Praxis lost is uncertain. It is not proof of failure. If
the old worker can still run, recovery needs positive confirmation that the work
stopped before Praxis moves it.

## Knowledge and effects

A context provider returns bounded context that carries its provenance.
NoesisContextProvider gives retrieval for noesis-kb-v1. PublicationService is
separate: it gates an export with the policy for a verified result, the status
of the candidate, a scoped authority and an idempotent receipt.

Retrieval never publishes automatically. To receive model output does not apply
an approved effect, and it does not publish knowledge.

## Observability

An export of public data uses redaction, metrics of finite cardinality and
correlations of traces. The journal stays privileged data for recovery.

Before you host native workloads, read the [threat model](threat-model.md), the
[isolation policy](isolation.md), the [secret integration](secrets.md), the
[version matrix](versions.md) and the [operations runbook](operations.md).
