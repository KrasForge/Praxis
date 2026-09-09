import asyncio
import json

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult
from praxis.knowledge.publication import NoesisExporter
from praxis.transport.http import TransportError


def test_noesis_export_preserves_unverified_state_and_lineage():
    captured = {}
    class Transport:
        error = None
        async def request(self, method, path, body=None):
            assert method == "POST" and path == "/documents/ingest"
            captured.update(body)
            if self.error is not None:
                raise self.error
            return {"document_id": body["document_id"]}
    async def exercise():
        result = ProcessResult("p", "a", State.FAILED, Outcome(OutcomeStatus.PARTIAL, "partial"))
        event = Event("p", "process.created", parent_id="parent")
        transport = Transport()
        exporter = NoesisExporter(transport)
        assert (await exporter.export(result, (event,))).status == "accepted"
        metadata = captured["metadata"]["praxis"]
        assert not metadata["verified"] and metadata["result"]["state"] == "failed"
        assert metadata["lineage"][0]["parent_id"] == "parent"
        assert json.loads(captured["content"]) == json.loads(result.to_json())
        transport.error = TransportError("http_error", 422)
        assert (await exporter.export(result)).status == "rejected"
        transport.error = TransportError("transport_unavailable")
        assert (await exporter.export(result)).status == "unavailable"
    asyncio.run(exercise())


def test_publication_policy_durable_duplicates(tmp_path):
    from dataclasses import replace

    from praxis.kernel.authority import Authority
    from praxis.kernel.capabilities import Resource
    from praxis.kernel.process import Process
    from praxis.kernel.spec import ProcessSpec
    from praxis.knowledge.policy import PublicationPolicy, PublicationService
    from praxis.storage.sqlite import SQLiteStore

    class Transport:
        calls = 0
        async def request(self, method, path, body=None):
            self.calls += 1
            return {"document_id": body["document_id"]}
    async def exercise():
        store = SQLiteStore(tmp_path / "db")
        process = Process(ProcessSpec("task", "fake"))
        process.move(State.FAILED)
        store.save(process)
        result = ProcessResult(process.process_id, process.attempt_id, State.FAILED, Outcome(OutcomeStatus.PARTIAL, "partial"))
        transport = Transport()
        exporter = NoesisExporter(transport)
        authority = Authority()
        service = PublicationService(store, authority, exporter)
        assert (await service.publish(result)).reason == "publication_ineligible"
        policy = PublicationPolicy(False, frozenset({State.FAILED}))
        service = PublicationService(store, authority, exporter, policy)
        assert (await service.publish(result)).reason == "publication_capability_denied"
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "noesis")
        assert (await service.publish(result)).status == "accepted"
        service = PublicationService(store, authority, exporter, policy)
        assert (await service.publish(result)).status == "duplicate"
        assert transport.calls == 1
        assert (await service.publish(replace(result, conclusion="changed"))).reason == "publication_idempotency_conflict"
        store.close()
    asyncio.run(exercise())
