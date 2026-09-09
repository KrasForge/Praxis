"""Typed authority claims; serialized claims do not themselves grant authority."""

import ipaddress
import json
from praxis.kernel.parsing import load_object
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
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
        object.__setattr__(self, "scope", normalize_scope(self.resource, self.scope))
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
            and scope_contains(self.resource, parent.scope, self.scope)
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
            data = load_object(raw)
            data["resource"] = Resource(data["resource"])
            if not isinstance(data["actions"], list) or len(data["actions"]) != len(set(data["actions"])):
                raise ValueError("actions must be a unique array")
            data["actions"] = frozenset(data["actions"])
            return cls(**data)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, RuntimeError) as exc:
            raise ValueError("invalid capability") from exc


def normalize_scope(resource: Resource, scope: str) -> str:
    if not isinstance(scope, str) or not scope or "\x00" in scope:
        raise ValueError("invalid resource scope")
    if scope == "*":
        return scope
    if resource == Resource.FILESYSTEM:
        path = Path(scope)
        if not path.is_absolute():
            raise ValueError("filesystem scope must be absolute")
        return str(path.resolve())
    if resource == Resource.NETWORK:
        wildcard = scope.startswith("*.")
        host = scope[2:] if wildcard else scope
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if wildcard:
                raise ValueError("IP wildcards are unsupported")
            return str(address)
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError("invalid network host scope")
        return ("*." if wildcard else "") + host
    if "*" in scope:
        raise ValueError("partial wildcard is unsupported")
    return scope


def scope_contains(resource: Resource, granted: str, requested: str) -> bool:
    try:
        granted = normalize_scope(resource, granted)
        requested = normalize_scope(resource, requested)
    except (ValueError, OSError, UnicodeError, RuntimeError):
        return False
    if granted == "*":
        return True
    if requested == "*":
        return False
    if resource == Resource.FILESYSTEM:
        return Path(requested).is_relative_to(Path(granted))
    if resource == Resource.NETWORK and granted.startswith("*."):
        return requested == granted or requested.removeprefix("*.").endswith(granted[1:])
    return granted == requested
