"""Noesis publication triggers: an explicit per-process request and an event-driven worker.

Both paths go through PublicationService, so eligibility (completed and verified), the
``effect/apply`` grant on ``noesis``, candidate selection and idempotency are enforced the
same way. The worker tails the durable event journal from a persisted cursor, so events
that arrive while the host is down are published after restart (the batch catch-up).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.events import Event
from praxis.kernel.results import ProcessResult
from praxis.kernel.runtime import Kernel
from praxis.knowledge.policy import PublicationService
from praxis.knowledge.publication import PublicationResult
from praxis.storage.sqlite import SQLiteStore

TARGET = "noesis"
logger = logging.getLogger("praxis.host.publication")


@dataclass(frozen=True)
class Publisher:
    kernel: Kernel
    store: SQLiteStore
    authority: Authority
    service: PublicationService

    def grant(self, process_id: str) -> None:
        if not self.authorize(process_id):
            self.authority.issue(process_id, Resource.EFFECT, frozenset({"apply"}), TARGET)

    def authorize(self, process_id: str) -> bool:
        return self.authority.authorize(process_id, Resource.EFFECT, "apply", TARGET).allowed

    async def publish(self, process_id: str) -> PublicationResult:
        result = self.kernel.result(process_id)
        history = tuple(entry.event for entry in self.store.read_events(process_id))
        # Lineage ends at the result it explains. Later events (grants, publication receipts)
        # would change the document digest and turn an idempotent replay into a conflict.
        end = max((i for i, e in enumerate(history) if e.type == "process.result"), default=len(history) - 1)
        return await self.service.publish(result, history[:end + 1])

    async def request(self, process_id: str, actor: str) -> PublicationResult:
        """An authenticated person or service asked for this result to become knowledge."""
        process = self.kernel.processes[process_id]
        self.kernel.events.append(Event(process_id, "host.publication_requested", {
            "actor": actor, "target": TARGET, "attempt_id": process.attempt_id}, parent_id=process.parent_id))
        self.grant(process_id)
        return await self.publish(process_id)


class PublicationWorker:
    def __init__(self, publisher: Publisher, *, interval: float = 1.0, max_backoff: float = 300.0,
                 clock: Callable[[], float] = time.time):
        self.publisher = publisher
        self.interval = interval
        self.max_backoff = max_backoff
        self._now = clock
        self._stopping = asyncio.Event()
        with publisher.store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS host_publication_cursor "
                               "(name TEXT PRIMARY KEY, cursor INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS host_publication_queue (process_id TEXT NOT NULL, "
                               "attempt_id TEXT NOT NULL, attempts INTEGER NOT NULL, due REAL NOT NULL, "
                               "PRIMARY KEY(process_id, attempt_id))")

    def cursor(self) -> int:
        with self.publisher.store._transaction() as connection:
            row = connection.execute("SELECT cursor FROM host_publication_cursor WHERE name='noesis'").fetchone()
        return 0 if row is None else int(row[0])

    def pending(self) -> tuple[tuple[str, str, int], ...]:
        with self.publisher.store._transaction() as connection:
            rows = connection.execute("SELECT process_id, attempt_id, attempts FROM host_publication_queue "
                                      "ORDER BY due, process_id").fetchall()
        return tuple((str(r[0]), str(r[1]), int(r[2])) for r in rows)

    def scan(self) -> int:
        """Queue every newly finished result that holds a Noesis grant; returns the count."""
        after = self.cursor()
        entries = self.publisher.store.read_events(after=after)
        if not entries:
            return 0
        queued = []
        for entry in entries:
            if entry.event.type != "process.result":
                continue
            result = ProcessResult.from_json(entry.event.payload["result"])
            if self.publisher.authorize(result.process_id):
                queued.append((result.process_id, result.attempt_id))
        with self.publisher.store._transaction() as connection:
            # Queue and cursor move together, so a crash can neither skip nor lose a result.
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("INSERT OR IGNORE INTO host_publication_queue VALUES (?,?,0,?)",
                                   [(p, a, self._now()) for p, a in queued])
            connection.execute("INSERT INTO host_publication_cursor VALUES ('noesis', ?) ON CONFLICT(name) "
                               "DO UPDATE SET cursor=excluded.cursor", (entries[-1].cursor,))
        return len(queued)

    async def drain(self) -> list[tuple[str, PublicationResult]]:
        with self.publisher.store._transaction() as connection:
            due = connection.execute("SELECT process_id, attempt_id, attempts FROM host_publication_queue "
                                     "WHERE due <= ? ORDER BY due, process_id", (self._now(),)).fetchall()
        done = []
        for process_id, attempt_id, attempts in due:
            process = self.publisher.kernel.processes.get(process_id)
            if process is None or process.attempt_id != attempt_id:
                self._forget(process_id, attempt_id)  # superseded by a retry; its own result is queued
                continue
            receipt = await self.publisher.publish(process_id)
            done.append((process_id, receipt))
            if receipt.status == "unavailable" and receipt.reason != "publication_in_progress":
                delay = min(self.max_backoff, self.interval * 2 ** attempts)
                with self.publisher.store._transaction() as connection:
                    connection.execute("UPDATE host_publication_queue SET attempts=?, due=? WHERE process_id=? "
                                       "AND attempt_id=?", (attempts + 1, self._now() + delay, process_id, attempt_id))
                logger.warning("noesis publication unavailable for %s (%s); retry %d in %.0fs",
                               process_id, receipt.reason, attempts + 1, delay)
                continue
            if receipt.reason == "publication_in_progress":
                # A controller crash left an uncertain write: operators reconcile it (docs/operations.md).
                logger.error("noesis publication for %s needs operator reconciliation", process_id)
            elif receipt.status == "rejected":
                # Failed or unverified work is refused by design; anything else deserves attention.
                level = logging.INFO if receipt.reason == "publication_ineligible" else logging.WARNING
                logger.log(level, "noesis publication for %s rejected (%s)", process_id, receipt.reason)
            self._forget(process_id, attempt_id)
        return done

    async def run_once(self) -> list[tuple[str, PublicationResult]]:
        self.scan()
        return await self.drain()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("publication worker iteration failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()

    def _forget(self, process_id: str, attempt_id: str) -> None:
        with self.publisher.store._transaction() as connection:
            connection.execute("DELETE FROM host_publication_queue WHERE process_id=? AND attempt_id=?",
                               (process_id, attempt_id))


def result_payload(receipt: PublicationResult) -> dict[str, Any]:
    return asdict(receipt)
