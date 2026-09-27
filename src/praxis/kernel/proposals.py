"""Effect proposals a workload writes into its workspace (ADR 0001).

A workload cannot stage an effect. It writes ``praxis.effect-proposal`` v1 documents
under ``.praxis/effects/``; the kernel reads them from the verified snapshot, only after
verification approved the work, and turns them into ``Effect`` records. Identities,
authority, status and version are the kernel's to assign, so a proposal that names any
of them is rejected. Everything under ``.praxis/`` is excluded from canonical publication.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from praxis.kernel.effects import (
    Effect, EffectKind, artifact_publish, file_write, git_commit, message_send,
)
from praxis.kernel.parsing import load_object

SCHEMA = "praxis.effect-proposal"
RESERVED_PREFIX = ".praxis/"
PROPOSAL_PREFIX = ".praxis/effects/"
MAX_PROPOSALS = 64
MAX_PROPOSAL_BYTES = 1048576
MAX_TOTAL_BYTES = 4194304
FIELDS = {"schema", "schema_version", "kind", "target", "payload"}
PAYLOADS = {
    EffectKind.FILE_WRITE: {"content_base64"},
    EffectKind.GIT_COMMIT: {"message", "expected_head"},
    EffectKind.MESSAGE_SEND: {"text"},
    EffectKind.ARTIFACT_PUBLISH: {"artifact_path", "media_type"},
}


class ProposalError(ValueError):
    code = "invalid_effect_proposal"

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{self.code}: {path}: {reason}")


@dataclass(frozen=True)
class EffectProposal:
    path: str
    kind: EffectKind
    target: str
    payload: dict[str, Any]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProposalError(self.path, "unsupported_version")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "EffectProposal":
        try:
            data = load_object(raw)
        except ValueError:
            raise ProposalError("", "invalid_json") from None
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "schema": SCHEMA, "schema_version": self.schema_version,
                "kind": self.kind.value, "target": self.target, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EffectProposal":
        path = data.get("path")
        if not isinstance(path, str) or not path.startswith(PROPOSAL_PREFIX):
            raise ProposalError(str(path), "invalid_path")
        body = {key: value for key, value in data.items() if key != "path"}
        return parse_proposal(path, json.dumps(body))

    def idempotency_key(self, process_id: str, attempt_id: str) -> str:
        """Deterministic, so staging the same proposal again finds the same effect."""
        return hashlib.sha256("\0".join((process_id, attempt_id, self.path)).encode()).hexdigest()

    def to_effect(self, process_id: str, attempt_id: str, snapshot_id: str,
                  files: dict[str, bytes] | None = None) -> Effect:
        """Build the kernel record. An artifact is bound to bytes in the verified snapshot."""
        if self.kind == EffectKind.FILE_WRITE:
            effect = file_write(process_id, attempt_id, self.target,
                                base64.b64decode(self.payload["content_base64"], validate=True))
        elif self.kind == EffectKind.GIT_COMMIT:
            effect = git_commit(process_id, attempt_id, self.target, self.payload["message"],
                                self.payload["expected_head"])
        elif self.kind == EffectKind.MESSAGE_SEND:
            effect = message_send(process_id, attempt_id, self.target, self.payload["text"])
        else:
            path = self.payload["artifact_path"]
            content = None if files is None else files.get(path)
            if content is None:
                raise ProposalError(self.path, "artifact_not_in_snapshot")
            effect = artifact_publish(process_id, attempt_id, self.target, f"{snapshot_id}:{path}",
                                      self.payload["media_type"])
            effect = _with_payload(effect, {**effect.payload, "metadata": {
                "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}})
        if "metadata" in self.payload:
            metadata = {**effect.payload.get("metadata", {}), "proposal": self.payload["metadata"]}
            effect = _with_payload(effect, {**effect.payload, "metadata": metadata})
        return Effect(effect.process_id, effect.attempt_id, effect.kind, effect.target, effect.payload,
                      effect.reversible, effect.authority,
                      idempotency_key=self.idempotency_key(process_id, attempt_id))


def _with_payload(effect: Effect, payload: dict[str, Any]) -> Effect:
    return Effect(effect.process_id, effect.attempt_id, effect.kind, effect.target, payload,
                  effect.reversible, effect.authority)


def parse_proposal(path: str, raw: str) -> EffectProposal:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_PROPOSAL_BYTES:
        raise ProposalError(path, "proposal_too_large")
    try:
        data = load_object(raw)
    except ValueError:
        raise ProposalError(path, "invalid_json") from None
    unknown = set(data) - FIELDS
    if unknown:
        raise ProposalError(path, "unknown_field")
    if data.get("schema") != SCHEMA:
        raise ProposalError(path, "unknown_schema")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ProposalError(path, "unsupported_version")
    try:
        kind = EffectKind(data.get("kind"))
    except ValueError:
        raise ProposalError(path, "unknown_kind") from None
    if kind not in PAYLOADS:
        raise ProposalError(path, "kind_not_proposable")
    target, payload = data.get("target"), data.get("payload")
    if not isinstance(target, str) or not target.strip() or "\x00" in target or len(target) > 1024:
        raise ProposalError(path, "invalid_target")
    if not isinstance(payload, dict):
        raise ProposalError(path, "invalid_payload")
    required = PAYLOADS[kind]
    if not required <= payload.keys() or not payload.keys() <= required | {"metadata"}:
        raise ProposalError(path, "invalid_payload")
    if any(not isinstance(payload[key], str) for key in required):
        raise ProposalError(path, "invalid_payload")
    if kind == EffectKind.FILE_WRITE:
        try:
            base64.b64decode(payload["content_base64"], validate=True)
        except ValueError:
            raise ProposalError(path, "invalid_payload") from None
        if not PurePosixPath(target).is_absolute():
            raise ProposalError(path, "invalid_target")
    if kind == EffectKind.ARTIFACT_PUBLISH and not _relative(payload["artifact_path"]):
        raise ProposalError(path, "invalid_artifact_path")
    if kind == EffectKind.ARTIFACT_PUBLISH and payload["artifact_path"].startswith(RESERVED_PREFIX):
        raise ProposalError(path, "invalid_artifact_path")
    return EffectProposal(path, kind, target, payload)


def proposals_from_files(files: tuple[tuple[str, bytes], ...]) -> tuple[EffectProposal, ...]:
    """Every proposal in a verified snapshot, all or nothing, in path order."""
    selected = sorted((path, content) for path, content in files
                      if path.startswith(PROPOSAL_PREFIX) and not path.endswith("/"))
    if len(selected) > MAX_PROPOSALS:
        raise ProposalError(PROPOSAL_PREFIX, "too_many_proposals")
    if sum(len(content) for _, content in selected) > MAX_TOTAL_BYTES:
        raise ProposalError(PROPOSAL_PREFIX, "proposals_too_large")
    proposals = []
    for path, content in selected:
        if not path.endswith(".json") or "/" in path[len(PROPOSAL_PREFIX):]:
            raise ProposalError(path, "unexpected_file")
        try:
            raw = content.decode()
        except UnicodeError:
            raise ProposalError(path, "invalid_json") from None
        proposals.append(parse_proposal(path, raw))
    return tuple(proposals)


def published(path: str) -> bool:
    """Whether a snapshot entry belongs in canonical publication."""
    return path != RESERVED_PREFIX and not path.startswith(RESERVED_PREFIX)


def _relative(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(path) and not parsed.is_absolute() and ".." not in parsed.parts and str(parsed) == path \
        and path != "." and "\x00" not in path
