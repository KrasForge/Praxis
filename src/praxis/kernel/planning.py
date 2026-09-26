"""Materialize a verified plan into processes and a stored graph, then run it (ADR 0003).

A plan does nothing by itself. ``materialize`` accepts only a planning process that
completed, whose contract required the ``plan`` validator, and whose verified snapshot
holds ``plan.json``; it reads the plan from that snapshot, never the live workspace.
A terminal process cannot have children, so each node becomes a root process owned by
the plan's submitter and linked to the plan by the stored graph, whose owner is the
planning process. Grants are issued only where the host allowlist admits the request.

There is no graph daemon: ``advance`` starts the nodes a graph resolution makes
runnable, cancels the ones it fails, and runs again whenever a node finishes.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Collection
from functools import partial
from typing import Any

from praxis.kernel.contracts import Contract
from praxis.kernel.events import Event
from praxis.kernel.graph import Dependency, ProcessGraph
from praxis.kernel.lifecycle import State
from praxis.kernel.plan import PLAN_FILE, Plan, PlanError, parse_plan
from praxis.kernel.runtime import Kernel
from praxis.storage.graphs import GraphStore
from praxis.validators.plan import AllowedGrant, grant_allowed
from praxis.workspaces.protocol import WorkspaceError

PLAN_VALIDATOR = "plan"


class PlanningError(ValueError):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


class PlanMaterializer:
    def __init__(self, kernel: Kernel, graphs: GraphStore, allowlist: Callable[[], Collection[AllowedGrant]]):
        self.kernel = kernel
        self.graphs = graphs
        self.allowlist = allowlist
        self.background: set[asyncio.Task[None]] = set()

    def verified_plan(self, process_id: str) -> Plan:
        process = self.kernel.processes.get(process_id)
        if process is None:
            raise PlanningError(404, "process_not_found")
        report = self.kernel.verification.get(process_id)
        if process.state != State.COMPLETED or report is None or report.approved is not True:
            raise PlanningError(409, "plan_not_verified")
        contract = Contract.from_json(json.dumps(process.spec.contract))
        checks = contract.invariants + contract.validators
        if PLAN_FILE not in contract.required_outputs or not any(
                check.validator == PLAN_VALIDATOR and check.required for check in checks):
            raise PlanningError(409, "plan_not_validated")
        try:
            snapshot = self.kernel.workspaces.load_snapshot(process_id, report.snapshot_id)
            digest = dict(snapshot.files)[PLAN_FILE]
            blob = (self.kernel.workspaces.root / "blobs" / digest).read_bytes()
            if hashlib.sha256(blob).hexdigest() != digest:
                raise WorkspaceError("corrupt plan blob")
            return parse_plan(blob.split(b"\n", 1)[1].decode())
        except (WorkspaceError, KeyError, OSError, UnicodeError, PlanError):
            raise PlanningError(409, "plan_unavailable") from None

    def materialized(self, process_id: str) -> dict[str, Any] | None:
        for event in reversed(self.kernel.events):
            if event.process_id == process_id and event.type == "plan.materialized":
                return dict(event.payload)
        return None

    def materialize(self, process_id: str, actor: str, owner: str | None) -> dict[str, Any]:
        """Create the nodes and the graph once; a second call returns the first result."""
        existing = self.materialized(process_id)
        if existing is not None:
            return {**existing, "duplicate": True}
        plan = self.verified_plan(process_id)
        planning = self.kernel.processes[process_id]
        nodes: dict[str, str] = {}
        for key, spec in plan.nodes:
            # Each node is its own root: a terminal planning process cannot spawn children.
            node = self.kernel.create(spec, submission_actor=owner or None)
            nodes[key] = node.process_id
        edges = tuple(Dependency(nodes[e.prerequisite], nodes[e.dependent], e.requirement, e.on_failure)
                      for e in plan.dependencies)
        graph = ProcessGraph(frozenset(nodes.values()), edges)
        self.graphs.create(graph, process_id)
        allowlist = self.allowlist()
        issued, refused = [], []
        for request in plan.requested_grants:
            if grant_allowed(request, allowlist):
                self.kernel.authority.issue(nodes[request.node], request.resource, request.actions, request.scope)
                issued.append(request.to_dict())
            else:
                refused.append(request.to_dict())
        payload = {"actor": actor, "attempt_id": planning.attempt_id, "graph_id": graph.graph_id,
                   "nodes": nodes, "grants_issued": issued, "grants_refused": refused}
        self.kernel.events.append(Event(process_id, "plan.materialized", payload, parent_id=planning.parent_id))
        return {**payload, "duplicate": False}

    def graph_of(self, process_id: str) -> ProcessGraph | None:
        record = self.materialized(process_id)
        return None if record is None else self.graphs.load(str(record["graph_id"]))

    async def advance(self, process_id: str) -> list[str]:
        """Start runnable nodes and cancel nodes whose prerequisites failed."""
        graph = self.graph_of(process_id)
        if graph is None:
            return []
        started = []
        states = {node: self.kernel.processes[node].state for node in graph.nodes}
        for node in graph.topological():
            if node in self.kernel.tasks or states[node] != State.PENDING:
                continue
            resolution = graph.resolve(node, states)
            if resolution.state == "runnable":
                self.kernel.start(node)
                self.kernel.tasks[node].add_done_callback(partial(self._finished, process_id))
                started.append(node)
            elif resolution.state == "failed":
                await self.kernel.cancel(node)
                states[node] = self.kernel.processes[node].state
        return started

    def _finished(self, process_id: str, _: "asyncio.Task[Any]") -> None:
        self._schedule(process_id)

    def _schedule(self, process_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self._advance_quietly(process_id))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def _advance_quietly(self, process_id: str) -> None:
        try:
            await self.advance(process_id)
        except Exception:
            # The next finished node, or the next recovery, advances the graph again.
            pass

    async def recover(self) -> None:
        """After a restart, continue every materialized plan."""
        roots = {event.process_id for event in self.kernel.events if event.type == "plan.materialized"}
        for process_id in sorted(roots):
            await self.advance(process_id)
