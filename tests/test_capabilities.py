from dataclasses import replace

import pytest

from praxis.kernel.capabilities import Capability, Resource


def test_roundtrip_and_subset():
    parent = Capability(Resource.EXECUTOR, frozenset({"execute", "control"}), "*", "kernel", "p", max_bytes=100)
    child = replace(parent, scope="local", actions=frozenset({"execute"}), max_bytes=50)
    assert Capability.from_json(parent.to_json()) == parent
    assert child.is_subset_of(parent)
    assert not parent.is_subset_of(child)
    assert not replace(child, max_bytes=None).is_subset_of(parent)
    assert not replace(child, resource=Resource.EFFECT, actions=frozenset({"apply"})).is_subset_of(parent)


@pytest.mark.parametrize("changes", [
    {"actions": frozenset()}, {"actions": frozenset({"root"})},
    {"scope": ""}, {"max_bytes": True}, {"schema_version": 2}, {"issued_at": "2026-01-01"},
])
def test_invalid_capabilities(changes):
    with pytest.raises(ValueError):
        Capability(**{"resource": Resource.EXECUTOR, "actions": frozenset({"execute"}),
                      "scope": "local", "issuer": "kernel", "recipient": "p", **changes})
