"""Versioned process graphs with typed dependency requirements."""

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from uuid import uuid4


class Requirement(str, Enum):
    SUCCESS = "success"
    TERMINAL = "terminal"


class DependencyFailure(str, Enum):
    BLOCK = "block"
    FAIL = "fail"
    CONTINUE = "continue"


@dataclass(frozen=True)
class Dependency:
    prerequisite: str
    dependent: str
    requirement: Requirement = Requirement.SUCCESS
    on_failure: DependencyFailure = DependencyFailure.BLOCK

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v for v in (self.prerequisite, self.dependent)):
            raise ValueError("dependency identities required")
        if self.prerequisite == self.dependent:
            raise ValueError("self dependency")
        if not isinstance(self.requirement, Requirement) or not isinstance(self.on_failure, DependencyFailure):
            raise ValueError("invalid dependency policy")


@dataclass(frozen=True)
class ProcessGraph:
    nodes: frozenset[str]
    edges: tuple[Dependency, ...] = ()
    graph_id: str = field(default_factory=lambda: str(uuid4()))
    version: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, frozenset) or any(not isinstance(node, str) or not node for node in self.nodes):
            raise ValueError("invalid graph nodes")
        if not isinstance(self.edges, tuple) or any(not isinstance(edge, Dependency) for edge in self.edges):
            raise ValueError("invalid graph edges")
        if any(edge.prerequisite not in self.nodes or edge.dependent not in self.nodes for edge in self.edges):
            raise ValueError("missing dependency node")
        if len({(e.prerequisite, e.dependent) for e in self.edges}) != len(self.edges):
            raise ValueError("duplicate dependency edge")
        if type(self.version) is not int or self.version < 0 or type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("invalid graph version")
        if not isinstance(self.graph_id, str) or not self.graph_id:
            raise ValueError("graph identity required")

    def to_json(self) -> str:
        data = asdict(self)
        data["nodes"] = sorted(self.nodes)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ProcessGraph":
        try:
            data = json.loads(raw)
            if not isinstance(data["nodes"], list) or len(set(data["nodes"])) != len(data["nodes"]):
                raise ValueError("invalid nodes")
            data["nodes"] = frozenset(data["nodes"])
            edges = []
            for edge in data.get("edges", []):
                edge["requirement"] = Requirement(edge.get("requirement", "success"))
                edge["on_failure"] = DependencyFailure(edge.get("on_failure", "block"))
                edges.append(Dependency(**edge))
            data["edges"] = tuple(edges)
            return cls(**data)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise ValueError("invalid process graph") from exc
