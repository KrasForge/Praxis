"""Versioned dispatch envelope; execution identity is deterministic per attempt."""

import json
from dataclasses import asdict, dataclass
from uuid import NAMESPACE_URL, uuid5

from praxis.kernel.lineage import Lineage
from praxis.kernel.parsing import load_object
from praxis.kernel.spec import ProcessSpec
from praxis.observability.tracing import TraceContext
from praxis.remote.bundle import WorkspaceBundle


def execution_id(process_id: str, attempt_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"praxis:{process_id}:{attempt_id}"))


@dataclass(frozen=True)
class Dispatch:
    worker_id: str
    generation: int
    process_id: str
    attempt_id: str
    executor: str
    spec_json: str
    workspace_json: str
    lineage_json: str
    parent_id: str | None = None
    protocol_version: int = 1
    trace_json: str | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v for v in (self.worker_id, self.process_id, self.attempt_id, self.executor)):
            raise ValueError("invalid_dispatch_identity")
        if type(self.generation) is not int or self.generation < 1 or type(self.protocol_version) is not int or self.protocol_version != 1:
            raise ValueError("incompatible_dispatch")
        if ProcessSpec.from_json(self.spec_json).capabilities:
            raise ValueError("dispatch_cannot_assert_authority")
        WorkspaceBundle.from_json(self.workspace_json)
        if self.trace_json is not None:
            TraceContext.from_json(self.trace_json)
        lineage = Lineage.from_json(self.lineage_json)
        if (lineage.process_id, lineage.attempt_id) != (self.process_id, self.attempt_id):
            raise ValueError("dispatch_lineage_mismatch")

    @property
    def execution_id(self) -> str:
        return execution_id(self.process_id, self.attempt_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Dispatch":
        try:
            return cls(**load_object(raw))
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid_dispatch") from exc
