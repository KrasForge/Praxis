"""Metric contract v1: bounded labels and aggregate-only observations."""

import math
from dataclasses import dataclass
from types import MappingProxyType

from praxis.executors.outcomes import OutcomeStatus
from praxis.kernel.budgets import RESOURCES
from praxis.kernel.effects import EffectKind, EffectStatus
from praxis.kernel.lifecycle import State

METRICS_VERSION = 1
LABEL_VALUES = MappingProxyType({
    "executor": frozenset({"fake", "local", "shell", "codex", "claude", "deepseek", "remote", "unknown"}),
    "state": frozenset(s.value for s in State),
    "outcome": frozenset(s.value for s in OutcomeStatus),
    "effect_kind": frozenset(s.value for s in EffectKind),
    "effect_status": frozenset(s.value for s in EffectStatus),
    "failure_class": frozenset({"transport", "executor", "contract", "policy", "budget", "unknown"}),
})


@dataclass(frozen=True)
class MetricDefinition:
    kind: str
    unit: str
    labels: frozenset[str] = frozenset()


CATALOG = MappingProxyType({
    "praxis_processes_created_total": MetricDefinition("counter", "processes", frozenset({"executor"})),
    "praxis_processes_current": MetricDefinition("gauge", "processes", frozenset({"state"})),
    "praxis_attempts_total": MetricDefinition("counter", "attempts", frozenset({"executor"})),
    "praxis_retries_total": MetricDefinition("counter", "attempts", frozenset({"executor"})),
    "praxis_failures_total": MetricDefinition("counter", "outcomes", frozenset({"failure_class"})),
    "praxis_executor_outcomes_total": MetricDefinition("counter", "outcomes", frozenset({"executor", "outcome"})),
    "praxis_queue_depth": MetricDefinition("gauge", "processes"),
    "praxis_queue_wait_seconds": MetricDefinition("histogram", "seconds", frozenset({"executor"})),
    "praxis_execution_seconds": MetricDefinition("histogram", "seconds", frozenset({"executor"})),
    "praxis_effects_total": MetricDefinition("counter", "transitions", frozenset({"effect_kind", "effect_status"})),
    **{"praxis_usage_" + resource + "_total": MetricDefinition("counter", resource) for resource in RESOURCES},
})


@dataclass(frozen=True)
class Aggregate:
    count: int
    total: float
    minimum: float
    maximum: float


class Metrics:
    def __init__(self) -> None:
        self.series: dict[tuple[str, tuple[tuple[str, str], ...]], Aggregate] = {}

    def emit(self, name: str, value: float, **labels: str) -> None:
        if name not in CATALOG:
            raise ValueError("unknown_metric")
        definition = CATALOG[name]
        if labels.keys() != definition.labels or any(v not in LABEL_VALUES[k] for k, v in labels.items()):
            raise ValueError("invalid_metric_labels")
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid_metric_value")
        key = (name, tuple(sorted(labels.items())))
        previous = self.series.get(key)
        if previous is None or definition.kind == "gauge":
            self.series[key] = Aggregate(1, value, value, value)
        else:
            self.series[key] = Aggregate(previous.count + 1, previous.total + value,
                                         min(previous.minimum, value), max(previous.maximum, value))

    def value(self, name: str, **labels: str) -> float:
        aggregate = self.series.get((name, tuple(sorted(labels.items()))))
        return 0 if aggregate is None else aggregate.total
