import pytest

from praxis.kernel.graph import Dependency, DependencyFailure, ProcessGraph
from praxis.kernel.lifecycle import State


def test_fanout_fanin_and_cycles():
    graph = ProcessGraph(frozenset({"root", "a", "b", "join"}), (
        Dependency("root", "a"), Dependency("root", "b"), Dependency("a", "join"), Dependency("b", "join"),
    ))
    assert graph.topological() == ("root", "a", "b", "join")
    states = {node: State.PENDING for node in graph.nodes}
    assert graph.resolve("root", states).state == "runnable"
    assert graph.resolve("join", states).state == "blocked"
    states["root"] = State.COMPLETED
    assert graph.resolve("a", states).state == graph.resolve("b", states).state == "runnable"
    states.update(a=State.COMPLETED, b=State.COMPLETED)
    assert graph.resolve("join", states).state == "runnable"
    with pytest.raises(ValueError, match="cycle"):
        ProcessGraph(graph.nodes, graph.edges + (Dependency("join", "root"),))


@pytest.mark.parametrize("policy,expected", [(DependencyFailure.BLOCK, "blocked"),
                                            (DependencyFailure.FAIL, "failed"),
                                            (DependencyFailure.CONTINUE, "runnable")])
def test_explicit_failed_prerequisite_policy(policy, expected):
    graph = ProcessGraph(frozenset({"p", "c"}), (Dependency("p", "c", on_failure=policy),))
    assert graph.resolve("c", {"p": State.FAILED, "c": State.PENDING}).state == expected
