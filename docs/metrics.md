# Metrics contract v1

The catalog in `praxis.observability.metrics.CATALOG` gives the stable names,
units, kinds and labels.

A counter of created processes counts logical processes. A counter of attempts
and of retries describes the attempts at execution. A metric for the current
state and for the depth of a queue is a gauge. The wait in a queue and the
duration of an execution are in seconds. Usage keeps the exact declared units of
a resource, and that includes integer micro-USD. An effect counter counts the
transitions by kind and status, apart from the outcomes of an executor.

Each label has a finite allowlist. Never put one of these in a metric label:

- The ID of a process, attempt, worker, event or capability
- A path
- A prompt
- An objective
- The text of an exception
- A URL
- A secret

An unknown or custom
name of an executor maps to `unknown`. A remote placement maps to `remote`, with
no alias for each attempt. An invalid label fails before Praxis emits any
sample.

A histogram observation keeps the count, the sum, the minimum and the maximum.
It does not keep an unbounded list of samples. An exporter can translate these
aggregates into the summary of its own backend. To change the metric contract,
you need an explicit review of the version. To rename a metric, or to change a
unit, is a breaking change.

`Kernel.metrics.snapshot()` projects the committed events through a stable
cursor, and it does so incrementally. Repeated scrapes do not count an event
twice, and a new runtime can rebuild the same metrics from its journal. This
also includes the effect events that the effect service writes directly.

The duration of an execution spans the invocation through the record of the
outcome, and it includes the overhead of verification. Usage stays a separate
ledger of resources, and it includes the corrections from a recovery.
