import asyncio
from types import SimpleNamespace

from praxis.executors.deepseek import DeepSeekExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_deepseek_fixture(tmp_path):
    captured = {}
    class Harness:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            captured["closed"] = True
        def run(self, prompt, session_id):
            captured["session"] = session_id
            return SimpleNamespace(finish_reason="completed", final_response="answer")
    async def exercise():
        provider = LocalWorkspaces(tmp_path / "workspaces")
        authority = Authority()
        adapter = DeepSeekExecutor(provider, authority, tmp_path / "homes",
                                   sdk=SimpleNamespace(DeepSeekHarness=Harness))
        handle = provider.create("p")
        request = ExecutionRequest("p", "a", ProcessSpec("task", "deepseek"),
                                   handle.workspace_id, provider.path_for(handle, "p"))
        assert (await adapter.start(request)).reason == "isolation_required"
        adapter.isolated_worker = True
        authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "deepseek")
        assert (await adapter.start(request)).applied
        assert (await adapter.collect_result("a")).status == OutcomeStatus.COMPLETED
        assert captured["session"] == "a" and captured["closed"]
        assert captured["cwd"] == str(request.workspace_path)
        assert not list((tmp_path / "homes").iterdir())
        assert not (await adapter.cancel("a")).supported
    asyncio.run(exercise())
