"""Capability-gated publication with durable idempotency and eligibility."""

import hashlib
import json
from dataclasses import asdict, dataclass

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult
from praxis.knowledge.publication import NoesisExporter, PublicationResult, publication_document
from praxis.storage.sqlite import SQLiteStore


@dataclass(frozen=True)
class PublicationPolicy:
    require_verified: bool = True
    allowed_states: frozenset[State] = frozenset({State.COMPLETED})

    def eligible(self, result: ProcessResult) -> bool:
        return result.state in self.allowed_states and (not self.require_verified or
            (result.verification is not None and result.verification.approved is True))


class PublicationService:
    def __init__(self, store: SQLiteStore, authority: Authority, exporter: NoesisExporter,
                 policy: PublicationPolicy = PublicationPolicy(), target: str = "noesis"):
        self.store = store
        self.authority = authority
        self.exporter = exporter
        self.policy = policy
        self.target = target
        with store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS publications ("
                               "key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT)")

    async def publish(self, result: ProcessResult, lineage: tuple[Event, ...] = ()) -> PublicationResult:
        process = self.store.load(result.process_id)
        if process.attempt_id != result.attempt_id or process.state != result.state:
            return PublicationResult("rejected", "publication_stale_result")
        if not self.policy.eligible(result):
            return PublicationResult("rejected", "publication_ineligible")
        if not self.authority.authorize(result.process_id, Resource.EFFECT, "apply", self.target).allowed:
            return PublicationResult("rejected", "publication_capability_denied")
        isolated = False
        for stored in self.store.read_events(result.process_id):
            if stored.event.type == "candidate.isolated":
                isolated = True
            elif stored.event.type == "candidate.released":
                isolated = False
        if isolated:
            return PublicationResult("rejected", "candidate_not_selected")
        document = publication_document(result, lineage)
        key = self.target + ":" + document["document_id"]
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT digest,receipt FROM publications WHERE key=?", (key,)).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    return PublicationResult("rejected", "publication_idempotency_conflict")
                if previous[1] is None:
                    return PublicationResult("unavailable", "publication_in_progress")
                receipt = PublicationResult(**json.loads(previous[1]))
                if receipt.status == "accepted":
                    return PublicationResult("duplicate", "publication_already_accepted", receipt.document_id)
                # An uncertain result can be replayed using Noesis's stable document upsert key.
                connection.execute("UPDATE publications SET receipt=NULL WHERE key=?", (key,))
            else:
                connection.execute("INSERT INTO publications VALUES(?,?,NULL)", (key, digest))
        receipt = await self.exporter.export(result, lineage)
        event = Event(result.process_id, "knowledge.published", {"target": self.target,
                      "attempt_id": result.attempt_id, "receipt": asdict(receipt)}, parent_id=process.parent_id)
        with self.store._transaction() as connection:
            connection.execute("UPDATE publications SET receipt=? WHERE key=?", (json.dumps(asdict(receipt)), key))
            connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)",
                               (event.event_id, event.process_id, event.to_json()))
        return receipt
