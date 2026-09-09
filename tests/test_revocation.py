import pytest

from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Resource


def test_revocation_preserves_history_and_revokes_descendants():
    authority = Authority()
    authority.configure_process("p")
    authority.configure_process("c", "p")
    authority.configure_process("g", "c")
    parent = authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "local")
    child = authority.delegate("p", "c", parent.capability_id, actions=parent.actions, scope="local")
    grandchild = authority.delegate("c", "g", child.capability_id, actions=child.actions, scope="local")
    assert authority.authorize("g", Resource.EXECUTOR, "execute", "local").allowed
    history = [event.to_json() for event in authority.events]
    authority.revoke(parent.capability_id, actor="kernel", reason="operator request")
    assert [event.to_json() for event in authority.events[:len(history)]] == history
    decision = authority.authorize("g", Resource.EXECUTOR, "execute", "local")
    assert not decision.allowed and decision.reason == "capability_revoked"
    assert grandchild.issuer == "c" and grandchild.recipient == "g"
    with pytest.raises(AuthorizationError):
        authority.delegate("p", "c", parent.capability_id, actions=parent.actions, scope="local")


def test_revoke_before_use_and_unauthorized_revoke():
    authority = Authority()
    capability = authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "local")
    with pytest.raises(AuthorizationError):
        authority.revoke(capability.capability_id, actor="other", reason="forged")
    authority.revoke(capability.capability_id, actor="p", reason="no longer needed")
    assert not authority.authorize("p", Resource.EXECUTOR, "execute", "local").allowed
