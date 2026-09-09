"""Durable effect staging and capability checks before adapter invocation."""

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effects import Effect, EffectKind, EffectStatus
from praxis.kernel.events import Event
from praxis.storage.protocol import StoreConflict, StoreError
from praxis.storage.sqlite import SQLiteStore


@dataclass(frozen=True)
class EffectReceipt:
    applied: bool
    reason: str
    external_id: str | None = None


class EffectAdapter(Protocol):
    async def apply(self, effect: Effect) -> EffectReceipt: ...


class EffectService:
    def __init__(self, store: SQLiteStore, authority: Authority,
                 adapters: dict[EffectKind, EffectAdapter] | None = None):
        self.store = store
        self.authority = authority
        self.adapters = dict(adapters or {})
        with store._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS effects (id TEXT PRIMARY KEY, "
                               "process_id TEXT NOT NULL REFERENCES processes(id), idempotency_key TEXT UNIQUE NOT NULL, "
                               "version INTEGER NOT NULL, body TEXT NOT NULL)")

    def load(self, effect_id: str) -> Effect:
        with self.store._transaction() as connection:
            row = connection.execute("SELECT body FROM effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise StoreError("effect_not_found")
            return Effect.from_json(row[0])

    def stage(self, effect: Effect) -> Effect:
        if effect.status != EffectStatus.PROPOSED or effect.version != 0:
            raise ValueError("only new proposed effects can be staged")
        process = self.store.load(effect.process_id)
        if process.attempt_id != effect.attempt_id:
            raise ValueError("effect_attempt_mismatch")
        stage = self.authority.authorize(effect.process_id, Resource.EFFECT, "stage", effect.target)
        required = self.authority.authorize(effect.process_id, effect.authority.resource,
                                            effect.authority.action, effect.authority.scope)
        stored = effect if stage.allowed and required.allowed else effect.move(EffectStatus.REJECTED)
        with self.store._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT body FROM effects WHERE idempotency_key=?", (effect.idempotency_key,)).fetchone()
            if existing is not None:
                if existing[0] != stored.to_json():
                    raise StoreConflict("effect_idempotency_conflict")
                return Effect.from_json(existing[0])
            connection.execute("INSERT INTO effects VALUES(?,?,?,?,?)", (
                stored.effect_id, stored.process_id, stored.idempotency_key, stored.version, stored.to_json(),
            ))
            self._event(connection, stored, required.capability_id)
        return stored

    def approve(self, effect_id: str, expected_version: int) -> Effect:
        effect = self.load(effect_id)
        self.authority.require(effect.process_id, Resource.EFFECT, "approve", effect.target)
        self.authority.require(effect.process_id, effect.authority.resource, effect.authority.action, effect.authority.scope)
        return self._move(effect, expected_version, EffectStatus.APPROVED)

    def _move(self, effect: Effect, expected_version: int, status: EffectStatus) -> Effect:
        if type(expected_version) is not int or effect.version != expected_version:
            raise StoreConflict("stale_effect_version")
        updated = effect.move(status)
        with self.store._transaction() as connection:
            changed = connection.execute("UPDATE effects SET version=?,body=? WHERE id=? AND version=?",
                                         (updated.version, updated.to_json(), effect.effect_id, expected_version)).rowcount
            if changed != 1:
                raise StoreConflict("stale_effect_version")
            self._event(connection, updated, None)
        return updated

    def _event(self, connection: sqlite3.Connection, effect: Effect, capability_id: str | None) -> None:
        parent = connection.execute("SELECT parent_id FROM processes WHERE id=?", (effect.process_id,)).fetchone()[0]
        event = Event(effect.process_id, f"effect.{effect.status.value}", {
            "effect_id": effect.effect_id, "attempt_id": effect.attempt_id,
            "effect": effect.to_json(), "capability_id": capability_id, "replay_safe": False,
        }, parent_id=parent, event_id=f"effect:{effect.effect_id}:{effect.version}")
        connection.execute("INSERT INTO events(event_id,process_id,body) VALUES(?,?,?)",
                           (event.event_id, effect.process_id, event.to_json()))

    async def apply(self, effect_id: str, expected_version: int) -> EffectReceipt:
        effect = self.load(effect_id)
        if effect.status != EffectStatus.APPROVED or effect.version != expected_version:
            return EffectReceipt(False, "effect_not_approved_or_stale")
        decision = self.authority.authorize(effect.process_id, effect.authority.resource,
                                            effect.authority.action, effect.authority.scope)
        if not decision.allowed:
            self._move(effect, expected_version, EffectStatus.REJECTED)
            return EffectReceipt(False, decision.reason)
        adapter = self.adapters.get(effect.kind)
        if adapter is None:
            return EffectReceipt(False, "effect_adapter_unavailable")
        applying = self._move(effect, expected_version, EffectStatus.APPLYING)
        try:
            receipt = await adapter.apply(applying)
        except Exception:
            # Unknown external state must not be retried implicitly.
            return EffectReceipt(False, "application_uncertain")
        self._move(applying, applying.version, EffectStatus.APPLIED if receipt.applied else EffectStatus.FAILED)
        return receipt
