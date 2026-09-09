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
