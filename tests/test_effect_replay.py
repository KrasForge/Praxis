import asyncio
from dataclasses import replace

import pytest

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effect_service import EffectReceipt, EffectService
from praxis.kernel.effects import EffectKind, EffectStatus, message_send
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore


class CrashAfterSend:
    def __init__(self):
        self.receipts = {}
        self.calls = 0

    async def apply(self, effect):
        self.calls += 1
        self.receipts[effect.idempotency_key] = EffectReceipt(True, "sent", "remote-1")
        raise ConnectionError("response lost after external application")

    async def lookup(self, idempotency_key):
        return self.receipts.get(idempotency_key)


def test_crash_after_effect_never_replays(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.db"
        store = SQLiteStore(path)
        process = Process(ProcessSpec("work", "fake"))
        store.save(process)
        authority = Authority()
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"stage", "approve"}), "channel")
        authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "message:channel")
        adapter = CrashAfterSend()
        service = EffectService(store, authority, {EffectKind.MESSAGE_SEND: adapter})
        original = message_send(process.process_id, process.attempt_id, "channel", "once")
        proposed = service.stage(original)
        approved = service.approve(proposed.effect_id, proposed.version)
        assert (await service.apply(approved.effect_id, approved.version)).reason == "application_uncertain"
        assert service.load(approved.effect_id).status == EffectStatus.APPLYING
        store.close()
        reopened = SQLiteStore(path)
        service = EffectService(reopened, authority, {EffectKind.MESSAGE_SEND: adapter})
        assert not (await service.apply(approved.effect_id, approved.version)).applied
        assert (await service.reconcile(approved.effect_id)).applied
        assert (await service.apply(approved.effect_id, approved.version)).external_id == "remote-1"
        assert service.stage(replace(original, effect_id="duplicate-request")).effect_id == original.effect_id
        assert adapter.calls == 1
        with pytest.raises(ValueError, match="irreversible"):
            service.rollback(approved.effect_id)
        reopened.close()
    asyncio.run(exercise())
