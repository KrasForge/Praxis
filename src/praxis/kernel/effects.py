"""Versioned external effects and explicit lifecycle transitions."""

import base64
import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any
from uuid import uuid4

from praxis.kernel.capabilities import ACTIONS, Resource, normalize_scope
from praxis.kernel.events import _validate_json


class EffectKind(str, Enum):
    FILE_WRITE = "file_write"
    GIT_COMMIT = "git_commit"
    MESSAGE_SEND = "message_send"
    ARTIFACT_PUBLISH = "artifact_publish"
    EXTERNAL = "external"


class EffectStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    REJECTED = "rejected"


EFFECT_TRANSITIONS = {
    EffectStatus.PROPOSED: frozenset({EffectStatus.APPROVED, EffectStatus.REJECTED}),
    EffectStatus.APPROVED: frozenset({EffectStatus.APPLYING, EffectStatus.REJECTED}),
    EffectStatus.APPLYING: frozenset({EffectStatus.APPLIED, EffectStatus.FAILED}),
    EffectStatus.APPLIED: frozenset(), EffectStatus.FAILED: frozenset(), EffectStatus.REJECTED: frozenset(),
}


@dataclass(frozen=True)
class EffectAuthority:
    resource: Resource
    action: str
    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.resource, Resource) or self.action not in ACTIONS[self.resource]:
            raise ValueError("invalid effect authority")
        object.__setattr__(self, "scope", normalize_scope(self.resource, self.scope))


@dataclass(frozen=True)
class Effect:
    process_id: str
    attempt_id: str
    kind: EffectKind
    target: str
    payload: dict[str, Any]
    reversible: bool
    authority: EffectAuthority
    effect_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str = field(default_factory=lambda: str(uuid4()))
    status: EffectStatus = EffectStatus.PROPOSED
    version: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value in (self.process_id, self.attempt_id, self.target, self.effect_id, self.idempotency_key):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("effect identity and target required")
        if not isinstance(self.kind, EffectKind) or not isinstance(self.status, EffectStatus):
            raise ValueError("invalid effect type or status")
        if type(self.reversible) is not bool or not isinstance(self.authority, EffectAuthority) or not isinstance(self.payload, dict):
            raise ValueError("invalid effect fields")
        if type(self.version) is not int or self.version < 0 or type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("invalid effect version")
        _validate_json(self.payload)
        self._validate_kind()

    def _validate_kind(self) -> None:
        if self.kind == EffectKind.EXTERNAL:
            return
        required = {
            EffectKind.FILE_WRITE: {"content_base64"},
            EffectKind.GIT_COMMIT: {"message", "expected_head"},
            EffectKind.MESSAGE_SEND: {"text"},
            EffectKind.ARTIFACT_PUBLISH: {"artifact_ref", "media_type"},
        }[self.kind]
        if not required <= self.payload.keys() or not self.payload.keys() <= required | {"metadata"}:
            raise ValueError("invalid typed effect payload")
        if any(not isinstance(self.payload[key], str) for key in required):
            raise ValueError("typed effect fields must be strings")
        if self.kind == EffectKind.FILE_WRITE:
            base64.b64decode(self.payload["content_base64"], validate=True)
            target = normalize_scope(Resource.FILESYSTEM, self.target)
            expected = EffectAuthority(Resource.FILESYSTEM, "write", target)
            if target != self.target or target == "*":
                raise ValueError("file effect target must be an absolute path")
        else:
            prefix = {EffectKind.GIT_COMMIT: "git", EffectKind.MESSAGE_SEND: "message",
                      EffectKind.ARTIFACT_PUBLISH: "artifact"}[self.kind]
            expected = EffectAuthority(Resource.EFFECT, "apply", f"{prefix}:{self.target}")
        if self.authority != expected:
            raise ValueError("effect authority does not match operation")
        if self.kind in (EffectKind.MESSAGE_SEND, EffectKind.ARTIFACT_PUBLISH) and self.reversible:
            raise ValueError("external publication is not implicitly reversible")

    def move(self, status: EffectStatus) -> "Effect":
        if not isinstance(status, EffectStatus) or status not in EFFECT_TRANSITIONS[self.status]:
            raise ValueError("invalid_effect_transition")
        return replace(self, status=status, version=self.version + 1)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Effect":
        try:
            data = json.loads(raw)
            data["kind"] = EffectKind(data["kind"])
            data["status"] = EffectStatus(data.get("status", "proposed"))
            authority = data["authority"]
            authority["resource"] = Resource(authority["resource"])
            data["authority"] = EffectAuthority(**authority)
            return cls(**data)
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise ValueError("invalid effect") from exc


def file_write(process_id: str, attempt_id: str, target: str, content: bytes) -> Effect:
    target = normalize_scope(Resource.FILESYSTEM, target)
    return Effect(process_id, attempt_id, EffectKind.FILE_WRITE, target,
                  {"content_base64": base64.b64encode(content).decode()}, True,
                  EffectAuthority(Resource.FILESYSTEM, "write", target))


def git_commit(process_id: str, attempt_id: str, repository: str, message: str, expected_head: str) -> Effect:
    return Effect(process_id, attempt_id, EffectKind.GIT_COMMIT, repository,
                  {"message": message, "expected_head": expected_head}, True,
                  EffectAuthority(Resource.EFFECT, "apply", f"git:{repository}"))


def message_send(process_id: str, attempt_id: str, destination: str, text: str) -> Effect:
    return Effect(process_id, attempt_id, EffectKind.MESSAGE_SEND, destination, {"text": text}, False,
                  EffectAuthority(Resource.EFFECT, "apply", f"message:{destination}"))


def artifact_publish(process_id: str, attempt_id: str, destination: str, artifact_ref: str, media_type: str) -> Effect:
    return Effect(process_id, attempt_id, EffectKind.ARTIFACT_PUBLISH, destination,
                  {"artifact_ref": artifact_ref, "media_type": media_type}, False,
                  EffectAuthority(Resource.EFFECT, "apply", f"artifact:{destination}"))
