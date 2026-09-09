"""Control-plane operations over a single kernel owner."""

import json
from dataclasses import asdict
from typing import Any

from praxis.kernel.lifecycle import TERMINAL
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec, SpecError
from praxis.storage.protocol import ProcessStore


class APIError(ValueError):
    def __init__(self, status: int, code: str, details: dict[str, Any] | None = None):
        self.status = status
        self.code = code
        self.details = details or {}
        super().__init__(code)

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "details": self.details}}


class ControlPlane:
    def __init__(self, kernel: Kernel):
        self.kernel = kernel

    def submit(self, data: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        try:
            spec = ProcessSpec.from_json(json.dumps(data, allow_nan=False))
        except (SpecError, ValueError, TypeError) as exc:
            raise APIError(422, "invalid_process_spec", exc.to_dict() if isinstance(exc, SpecError) else {}) from None
        if idempotency_key is not None:
            if not idempotency_key or len(idempotency_key) > 200:
                raise APIError(422, "invalid_idempotency_key")
            if not isinstance(self.kernel.records, ProcessStore):
                raise APIError(503, "durable_submission_unavailable")
            for stored in self.kernel.records.read_events():
                event = stored.event
                if event.type == "process.created" and event.payload.get("submission_key") == idempotency_key:
                    previous = self.kernel.records.load(event.process_id)
                    if previous.spec.to_json() != spec.to_json():
                        raise APIError(409, "submission_idempotency_conflict")
                    return {"process_id": previous.process_id, "state": previous.state.value, "duplicate": True}
        try:
            process = self.kernel.create(spec, submission_key=idempotency_key)
        except ValueError:
            raise APIError(422, "submission_rejected") from None
        self.kernel.start(process.process_id)
        return {"process_id": process.process_id, "state": process.state.value, "duplicate": False}

    def inspect(self, process_id: str) -> dict[str, Any]:
        process = self.kernel.processes.get(process_id)
        if process is None:
            raise APIError(404, "process_not_found")
        history = (tuple(entry.event for entry in self.kernel.records.read_events(process_id))
                   if isinstance(self.kernel.records, ProcessStore) else
                   tuple(e for e in self.kernel.events if e.process_id == process_id))
        effects = {}
        for event in history:
            if event.type.startswith("effect.") and "effect" in event.payload:
                effect = json.loads(event.payload["effect"])
                effects[effect["effect_id"]] = effect
        result = None
        if process.state in TERMINAL and process_id in self.kernel.results:
            result = json.loads(self.kernel.result(process_id).to_json())
        report = self.kernel.verification.get(process_id)
        return {"process_id": process_id, "attempt_id": process.attempt_id, "state": process.state.value,
                "parent_id": process.parent_id, "children": sorted(p.process_id for p in self.kernel.processes.values()
                    if p.parent_id == process_id), "spec": json.loads(process.spec.to_json()),
                "result": result, "verification": None if report is None else json.loads(json.dumps(asdict(report))),
                "effects": list(effects.values()), "usage": self.kernel.usage.total(process_id),
                "last_event": None if not history else json.loads(history[-1].to_json())}

    def inspect_tree(self, process_id: str) -> dict[str, Any]:
        pending = [process_id]
        processes = []
        seen = set()
        while pending:
            identity = pending.pop(0)
            if identity in seen:
                raise APIError(500, "process_tree_corrupt")
            seen.add(identity)
            process = self.inspect(identity)
            processes.append(process)
            pending.extend(process["children"])
        return {"root": process_id, "processes": processes}
