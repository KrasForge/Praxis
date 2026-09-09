"""Control-plane operations over a single kernel owner."""

import json
from typing import Any

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
