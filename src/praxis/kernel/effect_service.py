"""Durable effect staging and capability checks before adapter invocation."""

import json
import sqlite3
from datetime import datetime
from enum import Enum
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effects import Effect, EffectKind, EffectStatus
from praxis.kernel.events import Event
from praxis.kernel.process import now
from praxis.storage.protocol import StoreConflict, StoreError
from praxis.storage.sqlite import SQLiteStore


@dataclass(frozen=True)
class EffectReceipt:
    applied: bool
    reason: str
    external_id: str | None = None


class EffectAdapter(Protocol):
    async def apply(self, effect: Effect) -> EffectReceipt: ...


@runtime_checkable
class ReconciliableEffectAdapter(EffectAdapter, Protocol):
    async def lookup(self, idempotency_key: str) -> EffectReceipt | None: ...


class EffectPolicy(str, Enum):
    AUTO = "auto"
    HUMAN = "human"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRecord:
    effect_id: str
    effect_version: int
    actor: str
    decision: str
    reason: str
    timestamp: str
    expires_at: str | None


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
            connection.execute("CREATE TABLE IF NOT EXISTS approvals (effect_id TEXT REFERENCES effects(id), "
                               "effect_version INTEGER, actor TEXT, decision TEXT, reason TEXT, timestamp TEXT, expires_at TEXT, "
                               "PRIMARY KEY(effect_id,effect_version))")
            connection.execute("CREATE TABLE IF NOT EXISTS effect_receipts (effect_id TEXT PRIMARY KEY REFERENCES effects(id), body TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS pending_approvals (effect_id TEXT PRIMARY KEY REFERENCES effects(id), version INTEGER)")

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
                previous = Effect.from_json(existing[0])
                before, after = asdict(previous), asdict(stored)
                for ignored in ("effect_id", "version", "status"):
                    before.pop(ignored)
                    after.pop(ignored)
                if before != after:
                    raise StoreConflict("effect_idempotency_conflict")
                return previous
            connection.execute("INSERT INTO effects VALUES(?,?,?,?,?)", (
                stored.effect_id, stored.process_id, stored.idempotency_key, stored.version, stored.to_json(),
            ))
            self._event(connection, stored, required.capability_id)
        return stored

    def approve(self, effect_id: str, expected_version: int, *, actor: str | None = None,
                reason: str = "configured policy", expires_at: str | None = None) -> Effect:
        return self.resolve_approval(effect_id, expected_version, True, actor=actor,
                                     reason=reason, expires_at=expires_at)

    def resolve_approval(self, effect_id: str, expected_version: int, approved: bool, *,
                         actor: str | None = None, reason: str,
                         expires_at: str | None = None) -> Effect:
        effect = self.load(effect_id)
        actor = effect.process_id if actor is None else actor
        if not actor or not reason.strip() or type(approved) is not bool:
            raise ValueError("approval actor, reason, and decision required")
        if expires_at is not None:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.utcoffset() is None or expiry <= datetime.fromisoformat(now()):
                raise ValueError("approval_expired")
        self.authority.require(actor, Resource.EFFECT, "approve", effect.target)
        if approved:
            self.authority.require(effect.process_id, effect.authority.resource, effect.authority.action, effect.authority.scope)
        if type(expected_version) is not int or effect.version != expected_version or effect.status != EffectStatus.PROPOSED:
            raise StoreConflict("stale_approval")
        updated = effect.move(EffectStatus.APPROVED if approved else EffectStatus.REJECTED)
        with self.store._transaction() as connection:
            changed = connection.execute("UPDATE effects SET version=?,body=? WHERE id=? AND version=?",
                                         (updated.version, updated.to_json(), effect_id, expected_version)).rowcount
            if changed != 1:
                raise StoreConflict("stale_approval")
            connection.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?)", (
                effect_id, updated.version, actor, "approved" if approved else "denied", reason, now(), expires_at,
            ))
            connection.execute("DELETE FROM pending_approvals WHERE effect_id=?", (effect_id,))
            self._event(connection, updated, None)
        return updated

    def apply_policy(self, effect_id: str, expected_version: int, policy: EffectPolicy,
                     *, actor: str, reason: str) -> Effect:
        if not isinstance(policy, EffectPolicy):
            raise ValueError("typed effect policy required")
        if policy == EffectPolicy.HUMAN:
            effect = self.load(effect_id)
            if effect.version != expected_version or effect.status != EffectStatus.PROPOSED:
                raise StoreConflict("stale_approval")
            with self.store._transaction() as connection:
                connection.execute("INSERT INTO pending_approvals VALUES(?,?) ON CONFLICT(effect_id) DO NOTHING",
                                   (effect_id, expected_version))
            return effect
        return self.resolve_approval(effect_id, expected_version, policy == EffectPolicy.AUTO, actor=actor, reason=reason)

    def pending(self) -> tuple[Effect, ...]:
        with self.store._transaction() as connection:
            rows = connection.execute("SELECT e.body FROM effects e JOIN pending_approvals p ON e.id=p.effect_id "
                                      "WHERE e.version=p.version ORDER BY e.id")
            return tuple(Effect.from_json(row[0]) for row in rows)

    def approvals(self, effect_id: str) -> tuple[ApprovalRecord, ...]:
        with self.store._transaction() as connection:
            return tuple(ApprovalRecord(*row) for row in connection.execute(
                "SELECT * FROM approvals WHERE effect_id=? ORDER BY effect_version", (effect_id,)))

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
        if effect.status == EffectStatus.APPLIED:
            receipt = self.receipt(effect_id)
            return receipt or EffectReceipt(False, "missing_application_receipt")
        if effect.status == EffectStatus.APPLYING:
            return EffectReceipt(False, "application_uncertain")
        if effect.status != EffectStatus.APPROVED or effect.version != expected_version:
            return EffectReceipt(False, "effect_not_approved_or_stale")
        approvals = self.approvals(effect_id)
        if not approvals or approvals[-1].effect_version != effect.version or approvals[-1].decision != "approved":
            return EffectReceipt(False, "approval_missing")
        expiry = approvals[-1].expires_at
        if expiry is not None and datetime.fromisoformat(expiry) <= datetime.fromisoformat(now()):
            self._move(effect, effect.version, EffectStatus.REJECTED)
            return EffectReceipt(False, "approval_expired")
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
        self._finish(applying, receipt)
        return receipt

    def _finish(self, applying: Effect, receipt: EffectReceipt) -> None:
        updated = applying.move(EffectStatus.APPLIED if receipt.applied else EffectStatus.FAILED)
        with self.store._transaction() as connection:
            changed = connection.execute("UPDATE effects SET version=?,body=? WHERE id=? AND version=?",
                                         (updated.version, updated.to_json(), applying.effect_id, applying.version)).rowcount
            if changed != 1:
                raise StoreConflict("stale_effect_version")
            connection.execute("INSERT INTO effect_receipts VALUES(?,?)", (applying.effect_id, json.dumps(asdict(receipt))))
            self._event(connection, updated, None)

    def receipt(self, effect_id: str) -> EffectReceipt | None:
        with self.store._transaction() as connection:
            row = connection.execute("SELECT body FROM effect_receipts WHERE effect_id=?", (effect_id,)).fetchone()
            return None if row is None else EffectReceipt(**json.loads(row[0]))

    async def reconcile(self, effect_id: str) -> EffectReceipt:
        effect = self.load(effect_id)
        receipt = self.receipt(effect_id)
        if receipt is not None:
            return receipt
        if effect.status != EffectStatus.APPLYING:
            return EffectReceipt(False, "effect_not_in_flight")
        adapter = self.adapters.get(effect.kind)
        if not isinstance(adapter, ReconciliableEffectAdapter):
            return EffectReceipt(False, "reconciliation_unavailable")
        try:
            receipt = await adapter.lookup(effect.idempotency_key)
        except Exception:
            return EffectReceipt(False, "reconciliation_unavailable")
        if receipt is None:
            return EffectReceipt(False, "application_uncertain")
        self._finish(effect, receipt)
        return receipt

    def rollback(self, effect_id: str) -> None:
        effect = self.load(effect_id)
        if not effect.reversible:
            raise ValueError("irreversible_effect")
        raise ValueError("explicit_compensating_effect_required")
