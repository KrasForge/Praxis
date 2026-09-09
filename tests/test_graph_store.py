import pytest

from praxis.kernel.graph import Dependency, ProcessGraph
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.graphs import GraphStore
from praxis.storage.protocol import StoreConflict
from praxis.storage.sqlite import SQLiteStore


def test_atomic_graph_mutations_and_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteStore(path)
    processes = [Process(ProcessSpec("node", "fake")) for _ in range(3)]
    for process in processes:
        store.save(process)
    a, b, c = [p.process_id for p in processes]
    graphs = GraphStore(store)
    graph = ProcessGraph(frozenset({a, b}), (Dependency(a, b),))
    graphs.create(graph, a)
    graph = graphs.mutate(graph.graph_id, 0, "add_node", node=c)
    graph = graphs.mutate(graph.graph_id, 1, "add_edge", edge=Dependency(b, c))
    before = graphs.load(graph.graph_id)
    with pytest.raises(ValueError, match="cycle"):
        graphs.mutate(graph.graph_id, 2, "add_edge", edge=Dependency(c, a))
    assert graphs.load(graph.graph_id) == before
    with pytest.raises(StoreConflict):
        graphs.mutate(graph.graph_id, 0, "remove_node", node=c)
    events = store.read_events(a)
    assert [event.event.payload["version"] for event in events] == [0, 1, 2]
    store.close()
    reopened = SQLiteStore(path)
    graphs = GraphStore(reopened)
    assert graphs.load(graph.graph_id) == graph
    graph = graphs.mutate(graph.graph_id, 2, "remove_edge", edge=Dependency(b, c))
    graph = graphs.mutate(graph.graph_id, 3, "remove_node", node=c)
    assert c not in graph.nodes and graph.version == 4
    reopened.close()
