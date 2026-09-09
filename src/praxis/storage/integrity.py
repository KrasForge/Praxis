"""Monotonic process updates prevent stale writers from rewriting history."""

from praxis.kernel.process import Process
from praxis.storage.protocol import StoreConflict


def validate_update(previous: Process, incoming: Process) -> None:
    if (previous.process_id != incoming.process_id or previous.parent_id != incoming.parent_id
            or previous.spec.to_json() != incoming.spec.to_json()):
        raise StoreConflict("process_identity_conflict")
    if incoming.history[:len(previous.history)] != previous.history:
        raise StoreConflict("stale_process_history")
