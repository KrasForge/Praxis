import pytest

from praxis.kernel.graph import Dependency, DependencyFailure, ProcessGraph, Requirement


def test_graph_edge_roundtrip():
    graph = ProcessGraph(frozenset({"p", "a", "b"}), (
        Dependency("p", "a"), Dependency("a", "b", Requirement.TERMINAL, DependencyFailure.CONTINUE),
    ))
    assert ProcessGraph.from_json(graph.to_json()) == graph


def test_missing_and_duplicate_edges_rejected():
    with pytest.raises(ValueError):
        ProcessGraph(frozenset({"p"}), (Dependency("p", "missing"),))
    edge = Dependency("p", "c")
    with pytest.raises(ValueError):
        ProcessGraph(frozenset({"p", "c"}), (edge, edge))
