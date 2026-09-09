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
        self.parents: dict[str, str | None] = {}
        self.revoked: set[str] = set()

    def configure_process(self, process_id: str, parent_id: str | None = None) -> None:
        if process_id in self.parents:
            raise ValueError("process already configured")
        if parent_id is not None and parent_id not in self.parents:
            raise ValueError("unknown parent")
        self.parents[process_id] = parent_id
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
        for capability in sorted(self.grants.values(), key=lambda cap: cap.capability_id):
            if capability.recipient != recipient or capability.resource != resource:
                continue
            if action not in capability.actions or not scope_contains(resource, capability.scope, target):
                continue
            invalid = self._invalid_chain(capability)
            if invalid is not None:
                decision = Decision(False, invalid)
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

    def delegate(
        self, parent_id: str, child_id: str, capability_id: str,
        *, actions: frozenset[str], scope: str, max_bytes: int | None = None,
        expires_at: str | None = None,
    ) -> Capability:
        parent = self.grants.get(capability_id)
        if parent is None or parent.recipient != parent_id:
            raise AuthorizationError("unknown_parent_capability")
        if child_id not in self.parents or self.parents[child_id] != parent_id:
            raise AuthorizationError("not_direct_child")
        invalid = self._invalid_chain(parent)
        if invalid is not None:
            raise AuthorizationError(invalid)
        child = Capability(
            parent.resource, actions, scope, parent_id, child_id, parent_id=capability_id,
            max_bytes=parent.max_bytes if max_bytes is None else max_bytes,
            expires_at=parent.expires_at if expires_at is None else expires_at,
        )
        if not child.is_subset_of(parent):
            raise AuthorizationError("delegation_widens_authority")
        self.grants[child.capability_id] = child
        self.events.append(Event(child_id, "capability.delegated", {
            "capability_id": child.capability_id, "parent_capability_id": capability_id,
            "issuer": parent_id, "recipient": child_id, "issued_at": child.issued_at,
        }, parent_id=parent_id))
        return child

    def _invalid_chain(self, capability: Capability) -> str | None:
        visited: set[str] = set()
        current = datetime.fromisoformat(now())
        while True:
            if capability.capability_id in visited:
                return "capability_lineage_cycle"
            visited.add(capability.capability_id)
            if capability.capability_id in self.revoked:
                return "capability_revoked"
            if capability.expires_at is not None and datetime.fromisoformat(capability.expires_at) <= current:
                return "capability_expired"
            if capability.parent_id is None:
                return None if capability.issuer == "kernel" else "forged_capability_provenance"
            parent = self.grants.get(capability.parent_id)
            if parent is None or parent.recipient != capability.issuer or not capability.is_subset_of(parent):
                return "broken_capability_lineage"
            if self.parents.get(capability.recipient) != capability.issuer:
                return "broken_process_lineage"
            capability = parent

    def revoke(self, capability_id: str, *, actor: str, reason: str) -> None:
        capability = self.grants.get(capability_id)
        if capability is None:
            raise AuthorizationError("unknown_capability")
        if actor not in ("kernel", capability.issuer, capability.recipient):
            raise AuthorizationError("revocation_denied")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("revocation reason required")
        if capability_id in self.revoked:
            return
        self.revoked.add(capability_id)
        self.events.append(Event(capability.recipient, "capability.revoked", {
            "capability_id": capability_id, "actor": actor, "reason": reason,
        }))
