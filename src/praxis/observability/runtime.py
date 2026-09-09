"""Cursor-based telemetry projection over committed lifecycle events."""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from praxis.kernel.events import Event
from praxis.observability.metrics import LABEL_VALUES, METRICS_VERSION, Metrics
from praxis.storage.protocol import StoredEvent


class RuntimeMetrics:
    def __init__(self, events: Callable[[int], tuple[StoredEvent, ...]], executor_for: Callable[[str], str]):
        self.events = events
        self.executor_for = executor_for
        self.collector = Metrics()
        self.cursor = 0
        self.states: dict[str, str] = {}
        self.executors: dict[str, str] = {}
        self.started: dict[str, str] = {}
        self.queued: set[str] = set()
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.collector.emit("praxis_queue_depth", 0)

    @staticmethod
    def executor(name: str) -> str:
        name = "remote" if name.startswith("remote:") else name
        return name if name in LABEL_VALUES["executor"] else "unknown"

    def refresh(self) -> Metrics:
        for entry in self.events(self.cursor):
            if entry.cursor <= self.cursor:
                continue
            self._apply(entry.event)
            self.cursor = entry.cursor
        return self.collector

    def _state(self, pid: str, state: str) -> None:
        self.states[pid] = state
        for value in LABEL_VALUES["state"]:
            self.collector.emit("praxis_processes_current", sum(s == value for s in self.states.values()), state=value)

    def _apply(self, event: Event) -> None:
        pid, payload = event.process_id, event.payload
        executor = self.executors.get(pid, self.executor(self.executor_for(pid)))
        metric = self.collector
        if event.type == "process.created" and pid not in self.states:
            metric.emit("praxis_processes_created_total", 1, executor=executor)
            self._state(pid, "pending")
        elif event.type == "process.state":
            state = payload["state"]
            if state == "failed" and self.states.get(pid) != "failed":
                outcome = self.outcomes.get(pid, {})
                reason = outcome.get("reason", "")
                category = ("contract" if outcome.get("status") == "completed" or "verification" in reason else
                            "budget" if "budget" in reason else "policy" if "capability" in reason else
                            "transport" if "transport" in reason or "unavailable" in reason or "orphan" in reason else
                            "executor" if outcome else "unknown")
                metric.emit("praxis_failures_total", 1, failure_class=category)
            self._state(pid, state)
        elif event.type == "process.retry":
            metric.emit("praxis_retries_total", 1, executor=executor)
            self.outcomes.pop(pid, None)
            self._state(pid, "pending")
        elif event.type == "scheduler.queued":
            self.queued.add(pid)
            metric.emit("praxis_queue_depth", len(self.queued))
        elif event.type == "scheduler.placed":
            executor = self.executor(payload["executor"])
            self.executors[pid] = executor
            if pid in self.queued:
                self.queued.remove(pid)
                metric.emit("praxis_queue_wait_seconds", payload.get("wait_seconds", 0), executor=executor)
            metric.emit("praxis_queue_depth", len(self.queued))
        elif event.type == "scheduler.blocked":
            self.queued.discard(pid)
            metric.emit("praxis_queue_depth", len(self.queued))
        elif event.type == "executor.invoked":
            executor = self.executor(payload["executor"])
            self.executors[pid] = executor
            self.started[pid] = event.timestamp
            metric.emit("praxis_attempts_total", 1, executor=executor)
        elif event.type == "process.outcome":
            self.outcomes[pid] = payload
            metric.emit("praxis_executor_outcomes_total", 1, executor=executor, outcome=payload["status"])
            started = self.started.pop(pid, None)
            if started is not None:
                seconds = max(0.0, (datetime.fromisoformat(event.timestamp) - datetime.fromisoformat(started)).total_seconds())
                metric.emit("praxis_execution_seconds", seconds, executor=executor)
        elif event.type == "usage.recorded":
            for resource, value in payload["values"].items():
                metric.emit("praxis_usage_" + resource + "_total", value)
        elif event.type.startswith("effect.") and "effect" in payload:
            effect = json.loads(payload["effect"])
            metric.emit("praxis_effects_total", 1, effect_kind=effect["kind"], effect_status=effect["status"])

    def snapshot(self) -> dict[str, Any]:
        metrics = self.refresh()
        return {"contract_version": METRICS_VERSION, "cursor": self.cursor,
                "series": [{"name": name, "labels": dict(labels), "count": aggregate.count,
                            "value": aggregate.total, "min": aggregate.minimum, "max": aggregate.maximum}
                           for (name, labels), aggregate in sorted(metrics.series.items())]}
