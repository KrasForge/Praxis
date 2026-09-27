"""``praxis.plan`` v1: a process graph proposed as the output of a process (ADR 0003).

A plan is data. Its nodes are ordinary ``ProcessSpec`` documents keyed by a short name,
its dependencies are the edges of a ``ProcessGraph``, and the grants it lists are only
requests: a node spec that carries ``capabilities`` is rejected (TM-1). A plan runs only
after verification approved it and a person materialized it.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from praxis.kernel.capabilities import ACTIONS, Resource, normalize_scope
from praxis.kernel.graph import Dependency, DependencyFailure, ProcessGraph, Requirement
from praxis.kernel.parsing import load_object
from praxis.kernel.spec import ProcessSpec, SpecError

SCHEMA = "praxis.plan"
PLAN_FILE = "plan.json"
MAX_NODES = 256
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
REQUESTABLE = frozenset({Resource.FILESYSTEM, Resource.NETWORK, Resource.SECRET, Resource.EFFECT})


class PlanError(ValueError):
    code = "invalid_plan"

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{self.code}: {field}: {reason}")


@dataclass(frozen=True)
class GrantRequest:
    node: str
    resource: Resource
    actions: frozenset[str]
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "resource": self.resource.value, "actions": sorted(self.actions),
                "scope": self.scope}


@dataclass(frozen=True)
class Plan:
    nodes: tuple[tuple[str, ProcessSpec], ...]
    dependencies: tuple[Dependency, ...] = ()
    requested_grants: tuple[GrantRequest, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise PlanError("schema_version", "unsupported_version")

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(key for key, _ in self.nodes)

    def graph(self) -> ProcessGraph:
        """The graph over node keys; raises on a cycle."""
        return ProcessGraph(self.keys, self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "schema_version": self.schema_version,
            "nodes": [{"key": key, "spec": _without_authority(spec)} for key, spec in self.nodes],
            "dependencies": [{"prerequisite": e.prerequisite, "dependent": e.dependent,
                              "requirement": e.requirement.value, "on_failure": e.on_failure.value}
                             for e in self.dependencies],
            "requested_grants": [grant.to_dict() for grant in self.requested_grants],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Plan":
        return parse_plan(raw)


def _without_authority(spec: ProcessSpec) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(spec.to_json())
    data.pop("capabilities", None)
    return data


def _closed(field: str, data: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PlanError(field, "expected_object")
    if set(data) - allowed:
        raise PlanError(field, "unknown_field")
    if required - set(data):
        raise PlanError(field, "missing_field")
    return data


def parse_plan(raw: str) -> Plan:
    try:
        data = load_object(raw)
    except ValueError:
        raise PlanError("plan", "invalid_json") from None
    _closed("plan", data, {"schema", "schema_version", "nodes", "dependencies", "requested_grants"},
            {"schema", "schema_version", "nodes"})
    if data["schema"] != SCHEMA:
        raise PlanError("schema", "unknown_schema")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PlanError("schema_version", "unsupported_version")
    raw_nodes = data["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > MAX_NODES:
        raise PlanError("nodes", f"expected 1..{MAX_NODES} nodes")
    nodes = []
    for index, item in enumerate(raw_nodes):
        field = f"nodes[{index}]"
        _closed(field, item, {"key", "spec"}, {"key", "spec"})
        if not isinstance(item["key"], str) or not KEY.fullmatch(item["key"]):
            raise PlanError(field + ".key", "expected_lowercase_key")
        if not isinstance(item["spec"], dict):
            raise PlanError(field + ".spec", "expected_object")
        if item["spec"].get("capabilities", []) != []:
            # A plan requests grants; it never carries them (TM-1).
            raise PlanError(field + ".spec.capabilities", "authority_in_spec")
        try:
            spec = ProcessSpec.from_json(json.dumps(item["spec"]))
        except SpecError as exc:
            raise PlanError(field + ".spec", f"{exc.field}: {exc.reason}") from None
        nodes.append((item["key"], spec))
    keys = [key for key, _ in nodes]
    if len(set(keys)) != len(keys):
        raise PlanError("nodes", "duplicate_key")
    edges = []
    raw_edges = data.get("dependencies", [])
    if not isinstance(raw_edges, list):
        raise PlanError("dependencies", "expected_array")
    for index, item in enumerate(raw_edges):
        field = f"dependencies[{index}]"
        _closed(field, item, {"prerequisite", "dependent", "requirement", "on_failure"}, {"prerequisite", "dependent"})
        try:
            edges.append(Dependency(item["prerequisite"], item["dependent"],
                                    Requirement(item.get("requirement", "success")),
                                    DependencyFailure(item.get("on_failure", "block"))))
        except (ValueError, TypeError):
            raise PlanError(field, "invalid_dependency") from None
    grants = []
    raw_grants = data.get("requested_grants", [])
    if not isinstance(raw_grants, list):
        raise PlanError("requested_grants", "expected_array")
    for index, item in enumerate(raw_grants):
        field = f"requested_grants[{index}]"
        _closed(field, item, {"node", "resource", "actions", "scope"}, {"node", "resource", "actions", "scope"})
        try:
            resource = Resource(item["resource"])
            actions = item["actions"]
            if (resource not in REQUESTABLE or not isinstance(actions, list) or not actions
                    or len(set(actions)) != len(actions) or not set(actions) <= ACTIONS[resource]):
                raise ValueError()
            scope = normalize_scope(resource, item["scope"])
        except (ValueError, TypeError, OSError, UnicodeError):
            raise PlanError(field, "invalid_grant_request") from None
        if item["node"] not in keys:
            raise PlanError(field + ".node", "unknown_node")
        grants.append(GrantRequest(item["node"], resource, frozenset(actions), scope))
    plan = Plan(tuple(nodes), tuple(edges), tuple(grants))
    try:
        plan.graph()
    except ValueError as exc:
        raise PlanError("dependencies", "dependency_cycle" if "cycle" in str(exc) else "invalid_graph") from None
    return plan
