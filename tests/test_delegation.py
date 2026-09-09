import pytest

from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Resource


def test_nested_delegation_and_no_implicit_authority(tmp_path):
    authority = Authority()
    authority.configure_process("p")
    authority.configure_process("c", "p")
    authority.configure_process("g", "c")
    root = authority.issue("p", Resource.FILESYSTEM, frozenset({"read", "write"}), str(tmp_path), max_bytes=100)
    assert not authority.authorize("c", Resource.FILESYSTEM, "read", str(tmp_path)).allowed
    child = authority.delegate("p", "c", root.capability_id, actions=frozenset({"read"}),
                               scope=str(tmp_path / "sub"), max_bytes=50)
    grandchild = authority.delegate("c", "g", child.capability_id, actions=frozenset({"read"}),
                                    scope=str(tmp_path / "sub" / "file"), max_bytes=20)
    assert authority.authorize("g", Resource.FILESYSTEM, "read", grandchild.scope, byte_count=20).allowed
    assert not authority.authorize("g", Resource.FILESYSTEM, "write", grandchild.scope, byte_count=1).allowed
    assert grandchild.parent_id == child.capability_id
    with pytest.raises(AuthorizationError):
        authority.delegate("p", "g", root.capability_id, actions=frozenset({"read"}), scope=str(tmp_path))
    with pytest.raises(AuthorizationError):
        authority.delegate("c", "g", child.capability_id, actions=frozenset({"write"}), scope=child.scope)
    with pytest.raises(AuthorizationError):
        authority.delegate("c", "g", child.capability_id, actions=frozenset({"read"}), scope=str(tmp_path))
    with pytest.raises(AuthorizationError):
        authority.delegate("c", "g", child.capability_id, actions=frozenset({"read"}), scope=child.scope, max_bytes=51)
