import asyncio

import pytest

from praxis.knowledge.context import ContextItem, ContextRequest, ContextResponse, FakeContextProvider


def test_context_provider_bounds():
    items = tuple(ContextItem(str(i), "source text", date, '{"provider":"fixture"}') for i, date in enumerate(
        ["2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"]))
    request = ContextRequest("query", (("domain", "docs"),), "2026-01-01T00:00:00+00:00", max_results=1)
    assert ContextRequest.from_json(request.to_json()) == request
    response = asyncio.run(FakeContextProvider(ContextResponse("available", items)).query(request))
    assert response.status == "partial" and response.items == (items[1],)
    unavailable = ContextResponse("unavailable", reason="offline")
    assert asyncio.run(FakeContextProvider(unavailable).query(request)) == unavailable
    with pytest.raises(ValueError):
        ContextRequest("query", max_bytes=0)
