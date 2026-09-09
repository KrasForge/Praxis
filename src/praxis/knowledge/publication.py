"""Lossless ProcessResult export through Noesis document-ingest-v1."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from praxis.kernel.events import Event
from praxis.kernel.results import ProcessResult
from praxis.transport.http import JSONTransport, TransportError


@dataclass(frozen=True)
class PublicationResult:
    status: str
    reason: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"accepted", "rejected", "unavailable", "duplicate"}:
            raise ValueError("invalid publication status")


def publication_document(result: ProcessResult, lineage: tuple[Event, ...]) -> dict[str, Any]:
    # Validate/copy nested caller state before crossing the adapter boundary.
    result = ProcessResult.from_json(result.to_json())
    identity = hashlib.sha256(f"{result.process_id}:{result.attempt_id}".encode()).hexdigest()
    return {"document_id": "praxis-" + identity, "source_type": "note", "language": "en",
            "source_id": "praxis:" + result.process_id,
            "title": "Praxis result: " + result.state.value,
            "content": result.to_json(), "metadata": {"praxis": {
                "result": json.loads(result.to_json()),
                "lineage": [json.loads(event.to_json()) for event in lineage],
                "verified": result.verification is not None and result.verification.approved,
                "schema_version": 1}}}


class NoesisExporter:
    def __init__(self, transport: JSONTransport, path: str = "/documents/ingest"):
        self.transport = transport
        self.path = path

    async def export(self, result: ProcessResult, lineage: tuple[Event, ...] = ()) -> PublicationResult:
        document = publication_document(result, lineage)
        try:
            reply = await self.transport.request("POST", self.path, document)
            if reply.get("document_id") != document["document_id"]:
                return PublicationResult("rejected", "noesis_receipt_mismatch")
            return PublicationResult("accepted", "noesis_ingested", document["document_id"])
        except TransportError as exc:
            rejected = exc.status is not None and 400 <= exc.status < 500 and exc.status not in {408, 429}
            return PublicationResult("rejected" if rejected else "unavailable", "noesis_" + exc.code,
                                     document["document_id"])
