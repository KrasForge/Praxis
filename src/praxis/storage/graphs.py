"""Atomic graph mutations and versioned graph events in the process database."""

from dataclasses import replace

from praxis.kernel.events import Event
from praxis.kernel.graph import Dependency, ProcessGraph
from praxis.storage.protocol import StoreConflict, StoreError
from praxis.storage.sqlite import SQLiteStore


class GraphStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        with store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS graphs (id TEXT PRIMARY KEY, "
                               "owner_id TEXT NOT NULL REFERENCES processes(id), version INTEGER NOT NULL, body TEXT NOT NULL)")

    def create(self, graph: ProcessGraph, owner_id: str) -> None:
        if graph.version != 0:
            raise StoreError("initial_graph_version_must_be_zero")
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            known = {row[0] for row in connection.execute("SELECT id FROM processes")}
            if owner_id not in known or not graph.nodes <= known:
                raise StoreError("missing_graph_process")
            connection.execute("INSERT INTO graphs VALUES(?,?,?,?)", (graph.graph_id, owner_id, 0, graph.to_json()))
            self._event(connection, graph, owner_id, "created")

    def load(self, graph_id: str) -> ProcessGraph:
        with self.store._transaction() as connection:
            row = connection.execute("SELECT body FROM graphs WHERE id=?", (graph_id,)).fetchone()
            if row is None:
                raise StoreError("graph_not_found")
            return ProcessGraph.from_json(row[0])

    def mutate(self, graph_id: str, expected_version: int, operation: str,
               *, node: str | None = None, edge: Dependency | None = None) -> ProcessGraph:
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT owner_id,version,body FROM graphs WHERE id=?", (graph_id,)).fetchone()
            if row is None:
                raise StoreError("graph_not_found")
            if type(expected_version) is not int or row[1] != expected_version:
                raise StoreConflict("stale_graph_version")
            graph = ProcessGraph.from_json(row[2])
            nodes, edges = set(graph.nodes), list(graph.edges)
            if operation == "add_node" and node is not None and edge is None:
                if node in nodes or connection.execute("SELECT id FROM processes WHERE id=?", (node,)).fetchone() is None:
                    raise StoreError("invalid_new_graph_node")
                nodes.add(node)
            elif operation == "remove_node" and node is not None and edge is None:
                if node not in nodes:
                    raise StoreError("missing_graph_node")
                nodes.remove(node)
                edges = [e for e in edges if node not in (e.prerequisite, e.dependent)]
            elif operation == "add_edge" and edge is not None and node is None:
                edges.append(edge)
            elif operation == "remove_edge" and edge is not None and node is None:
                if edge not in edges:
                    raise StoreError("missing_graph_edge")
                edges.remove(edge)
            else:
                raise StoreError("invalid_graph_mutation")
            updated = replace(graph, nodes=frozenset(nodes), edges=tuple(edges), version=graph.version + 1)
            connection.execute("UPDATE graphs SET version=?,body=? WHERE id=?", (updated.version, updated.to_json(), graph_id))
            self._event(connection, updated, row[0], operation)
            return updated

    def _event(self, connection: "sqlite3.Connection", graph: ProcessGraph, owner_id: str, operation: str) -> None:
        owner = connection.execute("SELECT parent_id FROM processes WHERE id=?", (owner_id,)).fetchone()
        event = Event(owner_id, "graph.mutated", {
            "graph_id": graph.graph_id, "version": graph.version, "operation": operation,
            "graph": graph.to_json(),
        }, parent_id=owner[0], event_id=f"graph:{graph.graph_id}:{graph.version}")
        connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)", (event.event_id, owner_id, event.to_json()))


import sqlite3  # noqa: E402
