import asyncio

from praxis.knowledge.context import ContextRequest
from praxis.knowledge.noesis import NoesisContextProvider
from praxis.transport.http import TransportError


def test_noesis_contract_mapping_and_unavailable():
    class Transport:
        fail = False
        async def request(self, method, path, body=None):
            assert method == "GET" and path.startswith("/api/v1/kb/docs/search?")
            assert "limit=1" in path and "q=hello+world" in path
            if self.fail:
                raise TransportError("transport_unavailable")
            return {"contract": "noesis-kb-v1", "domain": "docs", "as_of_ms": 1784700000000,
                    "data": [{"id": "doc1", "title": "Evidence", "url": "https://example.org/source"},
                             {"id": "doc2", "title": "More evidence"}]}
    async def exercise():
        transport = Transport()
        provider = NoesisContextProvider(transport)
        request = ContextRequest("hello world", (("domain", "docs"),), max_results=1)
        response = await provider.query(request)
        assert response.status == "partial" and response.items[0].source_id == "doc1"
        assert "https://example.org/source" in response.items[0].provenance_json
        transport.fail = True
        assert (await provider.query(request)).reason == "noesis_transport_unavailable"
    asyncio.run(exercise())
