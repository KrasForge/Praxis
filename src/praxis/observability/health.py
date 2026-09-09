"""Read-only operational snapshots; no probes that launch execution or renew leases."""

from typing import Any

from praxis.kernel.budgets import RESOURCES
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.runtime import Kernel
from praxis.kernel.scheduler import Scheduler
from praxis.remote.heartbeat import Heartbeats
from praxis.storage.protocol import ProcessStore


class RuntimeHealth:
    def __init__(self, kernel: Kernel, scheduler: Scheduler | None = None, workers: Heartbeats | None = None):
        self.kernel, self.scheduler, self.workers = kernel, scheduler, workers

    def blocking_reason(self, process_id: str) -> str | None:
        process = self.kernel.processes[process_id]
        if process.state in TERMINAL:
            return None
        if process.state == State.SUSPENDED:
            return "suspended"
        history = (tuple(e.event for e in self.kernel.records.read_events(process_id))
                   if isinstance(self.kernel.records, ProcessStore) else tuple(self.kernel.events))
        for event in reversed(history):
            if event.process_id != process_id:
                continue
            if event.type in {"scheduler.blocked", "process.recovery"}:
                return str(event.payload.get("reason", event.type))
            if event.type in {"executor.invoked", "process.retry"}:
                break
        if process.state == State.PENDING:
            return "queued" if self.scheduler and process_id in self.scheduler.queue.entries else "awaiting_dispatch"
        return None

    def snapshot(self) -> dict[str, Any]:
        blocked = []
        pressure = []
        for identity, process in self.kernel.processes.items():
            reason = self.blocking_reason(identity)
            if reason:
                blocked.append({"process_id": identity, "reason": reason})
            if process.state not in TERMINAL:
                usage = self.kernel.usage.total(identity)
                for resource in sorted(RESOURCES):
                    limit = process.spec.budget.get(resource)
                    used = usage.get(resource, 0)
                    if limit is not None and (limit == 0 or used >= limit * 0.9):
                        pressure.append({"process_id": identity, "resource": resource, "used": used, "limit": limit})
        executors = {name: {"status": "healthy", "active": 0} for name in self.kernel.executors}
        queue: dict[str, Any] = {"depth": 0, "status": "healthy"}
        if self.scheduler:
            queue["depth"] = len(self.scheduler.queue.entries)
            for name, active in self.scheduler.active.items():
                if name not in executors:
                    continue
                executors[name] = {"active": active, "status": "saturated" if self.scheduler.slots[name].locked() else "healthy"}
            if self.scheduler.global_slots.locked():
                queue["status"] = "saturated"
        workers = {}
        if self.workers:
            available = self.workers.available(include_saturated=True)
            with self.workers.registry.store._transaction() as connection:
                identities = [row[0] for row in connection.execute("SELECT id FROM workers")]
            for identity in identities:
                cap = available.get(identity)
                workers[identity] = {"status": "unavailable" if cap is None else "saturated" if cap.capacity == 0 else "healthy"}
        statuses = [v["status"] for v in executors.values()] + [v["status"] for v in workers.values()]
        status = "healthy"
        if not executors and not any(v["status"] != "unavailable" for v in workers.values()):
            status = "unavailable"
        elif "unavailable" in statuses or blocked or pressure:
            status = "degraded"
        elif queue["status"] == "saturated" or (statuses and all(s == "saturated" for s in statuses)):
            status = "saturated"
        return {"status": status, "queue": queue, "executors": executors, "workers": workers,
                "blocked_processes": blocked, "budget_pressure": pressure}
