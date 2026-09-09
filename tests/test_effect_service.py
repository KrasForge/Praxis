import asyncio

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effect_service import EffectReceipt, EffectService
from praxis.kernel.effects import EffectKind, EffectStatus, message_send
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore


class RecordingAdapter:
    def __init__(self, store):
        self.store = store
        self.calls = []

    async def apply(self, effect):
        assert any(e.event.type == "effect.applying" for e in self.store.read_events(effect.process_id))
        self.calls.append(effect.effect_id)
        return EffectReceipt(True, "sent", "external-id")


def test_staging_checks_and_revocation_before_application(tmp_path):
    async def exercise():
        store = SQLiteStore(tmp_path / "runtime.db")
        process = Process(ProcessSpec("work", "fake"))
        store.save(process)
        authority = Authority()
        adapter = RecordingAdapter(store)
        service = EffectService(store, authority, {EffectKind.MESSAGE_SEND: adapter})
        denied = service.stage(message_send(process.process_id, process.attempt_id, "channel", "denied"))
        assert denied.status == EffectStatus.REJECTED
        assert not (await service.apply(denied.effect_id, denied.version)).applied
        assert not adapter.calls
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"stage", "approve"}), "channel")
        grant = authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "message:channel")
        staged = service.stage(message_send(process.process_id, process.attempt_id, "channel", "allowed"))
        approved = service.approve(staged.effect_id, staged.version)
        assert (await service.apply(approved.effect_id, approved.version)).applied
        assert len(adapter.calls) == 1
        another = service.stage(message_send(process.process_id, process.attempt_id, "channel", "revoked"))
        approved = service.approve(another.effect_id, another.version)
        authority.revoke(grant.capability_id, actor="kernel", reason="revoked")
        assert not (await service.apply(approved.effect_id, approved.version)).applied
        assert len(adapter.calls) == 1
        store.close()
    asyncio.run(exercise())


def test_candidate_effect_hold_survives_service_restart(tmp_path):
    from praxis.kernel.events import Event

    async def exercise():
        store = SQLiteStore(tmp_path / "runtime.db")
        process = Process(ProcessSpec("candidate", "fake"))
        store.save(process, (Event(process.process_id, "candidate.isolated", {"group_id": "g"}),))
        authority = Authority()
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"stage", "approve"}), "channel")
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "message:channel")
        adapter = RecordingAdapter(store)
        service = EffectService(store, authority, {EffectKind.MESSAGE_SEND: adapter})
        staged = service.stage(message_send(process.process_id, process.attempt_id, "channel", "hold"))
        approved = service.approve(staged.effect_id, staged.version)
        service = EffectService(store, authority, {EffectKind.MESSAGE_SEND: adapter})
        assert (await service.apply(approved.effect_id, approved.version)).reason == "candidate_not_selected"
        assert not adapter.calls
        assert service.load(approved.effect_id).status == EffectStatus.APPROVED
        store.close()
    asyncio.run(exercise())
