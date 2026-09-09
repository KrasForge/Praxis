"""Kernel-owned authority registry and one audited authorization path."""

from dataclasses import dataclass
from datetime import datetime

from praxis.kernel.capabilities import Capability, Resource, scope_contains
from praxis.kernel.events import Event
from praxis.kernel.process import now


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    capability_id: str | None = None


class AuthorizationError(PermissionError):
    code = "capability_denied"


class Authority:
    def __init__(self, *, execution_defaults: frozenset[str] = frozenset()):
        self.execution_defaults = execution_defaults
        self.grants: dict[str, Capability] = {}
        self.events: list[Event] = []

    def configure_process(self, process_id: str) -> None:
        for executor in sorted(self.execution_defaults):
            self.issue(process_id, Resource.EXECUTOR, frozenset({"execute", "control"}), executor)
        if self.execution_defaults:
            self.issue(process_id, Resource.WORKSPACE,
                       frozenset({"create", "inspect", "snapshot", "destroy"}), process_id)

    def issue(
        self, recipient: str, resource: Resource, actions: frozenset[str], scope: str,
        *, expires_at: str | None = None, max_bytes: int | None = None,
    ) -> Capability:
        capability = Capability(resource, actions, scope, "kernel", recipient,
                                expires_at=expires_at, max_bytes=max_bytes)
        self.grants[capability.capability_id] = capability
        self.events.append(Event(recipient, "capability.issued", {
            "capability_id": capability.capability_id,
        }))
        return capability

    def authorize(
        self, recipient: str, resource: Resource, action: str, target: str,
        *, byte_count: int | None = None,
    ) -> Decision:
        decision = Decision(False, "no_matching_capability")
        current = datetime.fromisoformat(now())
        for capability in sorted(self.grants.values(), key=lambda cap: cap.capability_id):
            if capability.recipient != recipient or capability.resource != resource:
                continue
            if action not in capability.actions or not scope_contains(resource, capability.scope, target):
                continue
            if capability.expires_at is not None and datetime.fromisoformat(capability.expires_at) <= current:
                decision = Decision(False, "capability_expired")
                continue
            if capability.max_bytes is not None and (
                type(byte_count) is not int or byte_count < 0 or byte_count > capability.max_bytes
            ):
                decision = Decision(False, "constraint_exceeded")
                continue
            decision = Decision(True, "allowed", capability.capability_id)
            break
        self.events.append(Event(recipient, "capability.decision", {
            "resource": resource.value, "action": action, "allowed": decision.allowed,
            "reason": decision.reason, "capability_id": decision.capability_id,
        }))
        return decision

    def require(self, recipient: str, resource: Resource, action: str, target: str) -> None:
        decision = self.authorize(recipient, resource, action, target)
        if not decision.allowed:
            raise AuthorizationError(decision.reason)
