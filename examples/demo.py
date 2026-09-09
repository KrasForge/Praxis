"""Offline examples: uv run python examples/demo.py. No provider calls or publication."""
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from praxis.api.service import ControlPlane
from praxis.executors.claude import ClaudeExecutor
from praxis.executors.fake import FakeExecutor
from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.graph import Dependency, ProcessGraph
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.knowledge.context import ContextRequest
from praxis.knowledge.noesis import NoesisContextProvider
from praxis.storage.graphs import GraphStore
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory


async def demo(root):
    workspaces = LocalWorkspaces(root / "workspaces")
    store = SQLiteStore(root / "runtime.db")
    authority = Authority(execution_defaults=frozenset({"local", "fake", "claude"}))
    class Result(SimpleNamespace):
        pass
    async def query(**kwargs):
        yield Result(subtype="success", is_error=False, result="fixture agent answer")
    sdk = SimpleNamespace(query=query, ResultMessage=Result, ClaudeAgentOptions=lambda **kw: kw)
    kernel = Kernel(store, workspaces, {
        "local": LocalProcessExecutor(workspaces), "fake": FakeExecutor(),
        # Fixture SDK has no native tools; live SDKs require an independently isolated worker.
        "claude": ClaudeExecutor(workspaces, authority, sdk, isolated_worker=True),
    }, authority=authority)
    canonical = CanonicalDirectory(root / "canonical")
    spec = ProcessSpec("write verified artifact", "local", inputs={"argv": [sys.executable, "-c",
        "from pathlib import Path; Path('answer.txt').write_text('verified output')"]},
        contract={"required_outputs": ["answer.txt"]})
    process = kernel.create(spec, canonical=canonical)
    authority.issue(process.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
    authority.issue(process.process_id, Resource.WORKSPACE, frozenset({"commit"}), process.process_id)
    kernel.start(process.process_id)
    await kernel.tasks[process.process_id]
    assert kernel.result(process.process_id).verification.approved
    assert (canonical.path / "answer.txt").read_text() == "verified output"
    print("local execution and verified commit: passed")

    first = kernel.create(ProcessSpec("prerequisite", "fake"))
    second = kernel.create(ProcessSpec("dependent", "fake"))
    graph = ProcessGraph(frozenset({first.process_id, second.process_id}), (Dependency(first.process_id, second.process_id),))
    GraphStore(store).create(graph, first.process_id)
    for identity in graph.topological():
        states = {pid: kernel.processes[pid].state for pid in graph.nodes}
        assert graph.resolve(identity, states).state == "runnable"
        kernel.start(identity)
        await kernel.tasks[identity]
    print("persisted graph dependencies: passed")

    agent = kernel.create(ProcessSpec("fixture agent", "claude"))
    kernel.start(agent.process_id)
    await kernel.tasks[agent.process_id]
    assert kernel.result(agent.process_id).outcome.stdout == "fixture agent answer"
    print("agent adapter fixture: passed")

    class NoesisFixture:
        async def request(self, method, path, body=None):
            return {"contract": "noesis-kb-v1", "domain": "docs", "as_of_ms": 1784700000000,
                    "data": [{"id": "document-1", "title": "Evidence", "content": "source text"}]}
    context = await NoesisContextProvider(NoesisFixture()).query(ContextRequest("evidence", (("domain", "docs"),)))
    assert context.items[0].source_id == "document-1"
    print("Noesis contract fixture: passed")

    # Modulo consumes these same inspection/event shapes over the authenticated HTTP API.
    service = ControlPlane(kernel)
    view = service.inspect(process.process_id)
    assert view["result"]["verification"]["approved"]
    assert service.events(process.process_id)
    print("Modulo inspection contract: passed")
    store.close()
    reopened = SQLiteStore(root / "runtime.db")
    recovered = Kernel(reopened, workspaces, {"fake": FakeExecutor()})
    recovered.recover_records()
    assert recovered.result(process.process_id).verification.approved
    reopened.close()
    print("restart inspection: passed")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="praxis-demo-") as directory:
        asyncio.run(demo(Path(directory)))
