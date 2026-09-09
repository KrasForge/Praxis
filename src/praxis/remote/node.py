"""Worker-side durable dispatch and result polling over a JSON RPC boundary."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ExecutionRequest, Executor
from praxis.kernel.events import Event
from praxis.kernel.spec import ProcessSpec
from praxis.remote.bundle import WorkspaceBundle
from praxis.remote.dispatch import Dispatch
from praxis.remote.workers import Worker, WorkerError
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


class WorkerNode:
    def __init__(self, worker: Worker, store: SQLiteStore, workspaces: LocalWorkspaces,
                 executors: dict[str, Executor], authenticate: Callable[[str], bool]):
        self.worker = worker
        self.store = store
        self.workspaces = workspaces
        self.executors = dict(executors)
        self.authenticate = authenticate
        self.tasks: dict[str, asyncio.Task[None]] = {}
        with store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS remote_jobs (id TEXT PRIMARY KEY, digest TEXT, dispatch TEXT, result TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS remote_events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT, body TEXT)")

    async def rpc(self, data: dict[str, Any], credential: str) -> dict[str, Any]:
        try:
            authorized = self.authenticate(credential) is True
        except Exception:
            authorized = False
        if not authorized:
            raise WorkerError("dispatch_unauthorized")
        operation = data.get("operation")
        if operation == "dispatch":
            dispatch = Dispatch.from_json(json.dumps(data["dispatch"]))
            if (dispatch.worker_id, dispatch.generation) != (self.worker.worker_id, self.worker.generation):
                raise WorkerError("stale_worker_generation")
            if dispatch.executor not in self.executors:
                raise WorkerError("remote_executor_unavailable")
            raw = dispatch.to_json()
            digest = hashlib.sha256(raw.encode()).hexdigest()
            with self.store._transaction() as connection:
                previous = connection.execute("SELECT digest FROM remote_jobs WHERE id=?", (dispatch.execution_id,)).fetchone()
                if previous is not None:
                    if previous[0] != digest:
                        raise WorkerError("dispatch_identity_conflict")
                    return {"accepted": True, "duplicate": True, "execution_id": dispatch.execution_id}
                connection.execute("INSERT INTO remote_jobs VALUES(?,?,?,NULL)", (dispatch.execution_id, digest, raw))
            self.tasks[dispatch.execution_id] = asyncio.create_task(self._run(dispatch))
            return {"accepted": True, "duplicate": False, "execution_id": dispatch.execution_id}
        if operation == "poll":
            identity = data.get("execution_id")
            after = data.get("after", 0)
            if type(after) is not int or after < 0:
                raise WorkerError("invalid_remote_cursor")
            with self.store._transaction() as connection:
                row = connection.execute("SELECT dispatch,result FROM remote_jobs WHERE id=?", (identity,)).fetchone()
                if row is None:
                    raise WorkerError("remote_execution_not_found")
                dispatch = Dispatch.from_json(row[0])
                events = [{"cursor": cursor, "event": json.loads(raw)} for cursor, raw in connection.execute(
                    "SELECT cursor,body FROM remote_events WHERE execution_id=? AND cursor>? ORDER BY cursor", (identity, after))]
            return {"execution_id": identity, "process_id": dispatch.process_id, "attempt_id": dispatch.attempt_id,
                    "generation": dispatch.generation, "events": events,
                    "result": None if row[1] is None else json.loads(row[1])}
        raise WorkerError("unsupported_worker_operation")

    def _event(self, dispatch: Dispatch, kind: str) -> Event:
        return Event(dispatch.process_id, kind, {"attempt_id": dispatch.attempt_id,
                     "worker_id": self.worker.worker_id, "generation": self.worker.generation,
                     "lineage": dispatch.lineage_json}, parent_id=dispatch.parent_id)

    async def _run(self, dispatch: Dispatch) -> None:
        handle = self.workspaces.create(dispatch.process_id)
        bundle = None
        try:
            WorkspaceBundle.from_json(dispatch.workspace_json).restore(self.workspaces, handle)
            spec = replace(ProcessSpec.from_json(dispatch.spec_json), executor=dispatch.executor)
            request = ExecutionRequest(dispatch.process_id, dispatch.attempt_id, spec, handle.workspace_id,
                                       self.workspaces.path_for(handle, dispatch.process_id),
                                       parent_id=dispatch.parent_id, lineage_json=dispatch.lineage_json)
            event = self._event(dispatch, "worker.execution_started")
            with self.store._transaction() as connection:
                connection.execute("INSERT INTO remote_events(execution_id,body) VALUES(?,?)", (dispatch.execution_id, event.to_json()))
            executor = self.executors[dispatch.executor]
            control = await executor.start(request)
            outcome = await executor.collect_result(dispatch.attempt_id) if control.applied else Outcome(OutcomeStatus.UNAVAILABLE, control.reason)
            bundle = json.loads(WorkspaceBundle.capture(self.workspaces, handle).to_json())
        except Exception:
            outcome = Outcome(OutcomeStatus.FAILED, "remote_executor_error")
        event = self._event(dispatch, "worker.execution_completed")
        result = {"outcome": json.loads(outcome.to_json()), "workspace": bundle}
        with self.store._transaction() as connection:
            connection.execute("UPDATE remote_jobs SET result=? WHERE id=?", (json.dumps(result), dispatch.execution_id))
            connection.execute("INSERT INTO remote_events(execution_id,body) VALUES(?,?)", (dispatch.execution_id, event.to_json()))
        self.workspaces.cleanup(handle)
