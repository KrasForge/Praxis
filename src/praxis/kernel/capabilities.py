"""Typed authority claims; serialized claims do not themselves grant authority."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from praxis.kernel.process import now


class Resource(str, Enum):
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    EXECUTOR = "executor"
    WORKSPACE = "workspace"
    SECRET = "secret"
    EFFECT = "effect"


ACTIONS = {
    Resource.FILESYSTEM: frozenset({"read", "write"}),
    Resource.NETWORK: frozenset({"connect", "listen"}),
    Resource.EXECUTOR: frozenset({"execute", "shell", "control"}),
    Resource.WORKSPACE: frozenset({"create", "inspect", "snapshot", "commit", "destroy"}),
    Resource.SECRET: frozenset({"read"}),
    Resource.EFFECT: frozenset({"stage", "apply", "approve"}),
}


@dataclass(frozen=True)
class Capability:
    resource: Resource
    actions: frozenset[str]
    scope: str
    issuer: str
    recipient: str
    capability_id: str = field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    issued_at: str = field(default_factory=now)
    expires_at: str | None = None
    max_bytes: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.resource, Resource):
            raise ValueError("unknown capability resource")
        if not isinstance(self.actions, frozenset) or not self.actions or not self.actions <= ACTIONS[self.resource]:
            raise ValueError("invalid capability actions")
        for value in (self.scope, self.issuer, self.recipient):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("capability scope and provenance required")
        UUID(self.capability_id)
        if self.parent_id is not None:
            UUID(self.parent_id)
            if self.parent_id == self.capability_id:
                raise ValueError("self-delegation")
        issued = datetime.fromisoformat(self.issued_at)
        if issued.utcoffset() is None:
            raise ValueError("issued_at must have timezone")
        if self.expires_at is not None:
            expires = datetime.fromisoformat(self.expires_at)
            if expires.utcoffset() is None or expires <= issued:
                raise ValueError("invalid capability expiration")
        if self.max_bytes is not None and (type(self.max_bytes) is not int or self.max_bytes < 0):
            raise ValueError("invalid byte constraint")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported capability version")

    def is_subset_of(self, parent: "Capability") -> bool:
        return (
            self.resource == parent.resource
            and self.actions <= parent.actions
            and (parent.scope == "*" or self.scope == parent.scope)
            and (parent.max_bytes is None or (
                self.max_bytes is not None and self.max_bytes <= parent.max_bytes
            ))
            and (parent.expires_at is None or (
                self.expires_at is not None
                and datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(parent.expires_at)
            ))
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["actions"] = sorted(self.actions)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Capability":
        try:
            data = json.loads(raw)
            data["resource"] = Resource(data["resource"])
            if not isinstance(data["actions"], list) or len(data["actions"]) != len(set(data["actions"])):
                raise ValueError("actions must be a unique array")
            data["actions"] = frozenset(data["actions"])
            return cls(**data)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("invalid capability") from exc
