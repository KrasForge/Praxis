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


@pytest.mark.parametrize("required,available", [(True, False), (False, False), (True, True)])
def test_context_dependency_execution(tmp_path, required, available):
    from praxis.executors.fake import FakeExecutor
    from praxis.executors.outcomes import OutcomeStatus
    from praxis.kernel.authority import Authority
    from praxis.kernel.runtime import Kernel
    from praxis.kernel.spec import ProcessSpec
    from praxis.storage.memory import MemoryStore
    from praxis.workspaces.local import LocalWorkspaces

    async def exercise():
        executor = FakeExecutor()
        response = ContextResponse("available", (ContextItem("source", "evidence", "2026-01-01T00:00:00+00:00", '{}'),)) if available else ContextResponse("unavailable", reason="offline")
        kernel = Kernel(MemoryStore(), LocalWorkspaces(tmp_path), {"fake": executor},
                        authority=Authority(execution_defaults=frozenset({"fake"})),
                        context_providers={"kb": FakeContextProvider(response)})
        process = kernel.create(ProcessSpec("task", "fake", context=[{
            "context_id": "facts", "provider": "kb", "required": required, "request": {"query": "facts"}}]))
        kernel.start(process.process_id)
        outcome = await kernel.tasks[process.process_id]
        if required and not available:
            assert outcome.reason == "required_context_unavailable" and not executor.requests
        else:
            assert outcome.status == OutcomeStatus.COMPLETED
            assert "facts" in executor.requests[process.attempt_id].spec.inputs["praxis.context"]
        assert "praxis.context" not in process.spec.inputs
        assert any(e.type == "context.resolved" and "lineage" in e.payload for e in kernel.events)
    asyncio.run(exercise())
