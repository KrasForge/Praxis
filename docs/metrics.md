# Metrics contract v1

The catalog in `praxis.observability.metrics.CATALOG` defines stable names,
units, kinds and labels. Created-process counters count logical processes;
attempt and retry counters describe execution attempts. Current-state and
queue-depth metrics are gauges. Queue wait and execution duration are seconds;
usage preserves exact declared resource units (including integer micro-USD).
Effects count transitions by kind/status, separately from executor outcomes.

Labels have finite allowlists. Never put process, attempt, worker, event or
capability IDs, paths, prompts, objectives, exception messages, URLs, or secrets
in a metric label. Unknown/custom executor names map to `unknown`; remote
placements map to `remote`, without per-attempt aliases. Invalid labels fail
before emitting any sample.

Histogram observations retain count, sum, minimum and maximum, rather than an
unbounded sample list. Exporters can translate these aggregates into their
backend's summary representation. Metric contract changes require an explicit
version review; renaming or changing a unit is a breaking change.
