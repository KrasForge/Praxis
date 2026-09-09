"""Stable artifact/evidence references with explicit availability and provenance."""

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum


class ReferenceKind(str, Enum):
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"


class Availability(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Reference:
    reference_id: str
    kind: ReferenceKind
    media_type: str
    process_id: str
    attempt_id: str
    uri: str | None = None
    availability: Availability = Availability.PRESENT
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value in (self.reference_id, self.media_type, self.process_id, self.attempt_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("reference identity, type, and provenance required")
        if not isinstance(self.kind, ReferenceKind) or not isinstance(self.availability, Availability):
            raise ValueError("invalid reference kind or availability")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported reference version")
        if self.uri is not None and (not isinstance(self.uri, str) or not self.uri):
            raise ValueError("invalid reference URI")
        if self.reference_id.startswith("sha256:"):
            digest = self.reference_id.removeprefix("sha256:")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid content address")

    @classmethod
    def from_content(cls, content: bytes, kind: ReferenceKind, media_type: str,
                     process_id: str, attempt_id: str, *, uri: str | None = None) -> "Reference":
        return cls("sha256:" + hashlib.sha256(content).hexdigest(), kind, media_type, process_id, attempt_id, uri)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Reference":
        try:
            data = json.loads(raw)
            data["kind"] = ReferenceKind(data["kind"])
            data["availability"] = Availability(data.get("availability", "present"))
            return cls(**data)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid reference") from exc
