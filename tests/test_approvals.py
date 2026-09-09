import pytest

from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effect_service import EffectPolicy, EffectService
from praxis.kernel.effects import EffectStatus, message_send
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.protocol import StoreConflict
from praxis.storage.sqlite import SQLiteStore


def test_human_policy_durable_approvals_and_staleness(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteStore(path)
    process = Process(ProcessSpec("work", "fake"))
    store.save(process)
    authority = Authority()
    authority.issue(process.process_id, Resource.EFFECT, frozenset({"stage"}), "channel")
    authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "message:channel")
    authority.issue("operator", Resource.EFFECT, frozenset({"approve"}), "channel")
    service = EffectService(store, authority)
    effect = service.stage(message_send(process.process_id, process.attempt_id, "channel", "text"))
    service.apply_policy(effect.effect_id, 0, EffectPolicy.HUMAN, actor="operator", reason="review")
    assert service.pending() == (effect,)
    approved = service.approve(effect.effect_id, 0, actor="operator", reason="reviewed")
    assert approved.status == EffectStatus.APPROVED
    assert not service.pending()
    with pytest.raises(StoreConflict):
        service.approve(effect.effect_id, 0, actor="operator", reason="stale")
    store.close()
    reopened = SQLiteStore(path)
    service = EffectService(reopened, authority)
    record = service.approvals(effect.effect_id)[0]
    assert record.actor == "operator" and record.reason == "reviewed"
    denied = service.stage(message_send(process.process_id, process.attempt_id, "channel", "deny"))
    assert service.apply_policy(denied.effect_id, 0, EffectPolicy.DENY, actor="operator", reason="policy").status == EffectStatus.REJECTED
    expired = service.stage(message_send(process.process_id, process.attempt_id, "channel", "expired"))
    with pytest.raises(ValueError, match="expired"):
        service.approve(expired.effect_id, 0, actor="operator", reason="old", expires_at="2000-01-01T00:00:00Z")
    reopened.close()
