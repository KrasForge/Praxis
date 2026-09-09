import asyncio
from types import SimpleNamespace

from praxis.executors.claude import ClaudeExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


class Result(SimpleNamespace):
    pass


def test_claude_sdk_fixture(tmp_path):
    async def exercise():
        captured = {}
        async def query(**kwargs):
            captured.update(kwargs)
            yield Result(subtype="success", is_error=False, result="answer")
        sdk = SimpleNamespace(query=query, ClaudeAgentOptions=lambda **kw: kw, ResultMessage=Result)
        provider = LocalWorkspaces(tmp_path)
        authority = Authority()
        adapter = ClaudeExecutor(provider, authority, sdk, isolated_worker=True)
        handle = provider.create("p")
        request = ExecutionRequest("p", "a", ProcessSpec("task", "claude"),
                                   handle.workspace_id, provider.path_for(handle, "p"))
        assert not (await adapter.start(request)).applied
        authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "claude")
        assert (await adapter.start(request)).applied
        assert (await adapter.start(request)).reason == "duplicate"
        assert (await adapter.collect_result("a")).status == OutcomeStatus.COMPLETED
        assert captured["options"]["cwd"] == str(request.workspace_path)
        assert captured["options"]["tools"] == []
        assert not (await adapter.cancel("a")).supported
        assert not (await adapter.checkpoint("a")).control.supported
    asyncio.run(exercise())
