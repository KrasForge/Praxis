import pytest

from praxis.kernel.capabilities import Capability, Resource, normalize_scope, scope_contains


def test_filesystem_boundaries(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    assert scope_contains(Resource.FILESYSTEM, str(root), str(root / "file"))
    assert not scope_contains(Resource.FILESYSTEM, str(root), str(root / ".." / "outside"))
    assert not scope_contains(Resource.FILESYSTEM, str(root), str(root / "escape" / "file"))
    assert not scope_contains(Resource.FILESYSTEM, str(root), str(root) + "-other/file")
    with pytest.raises(ValueError):
        normalize_scope(Resource.FILESYSTEM, "relative")


@pytest.mark.parametrize("host,allowed", [
    ("a.example.com", True), ("A.EXAMPLE.COM.", True), ("example.com", False),
    ("evil-example.com", False), ("example.com.evil", False),
    ("user@example.com", False), ("a.example.com/path", False),
    ("a%2eexample.com", False), ("*.sub.example.com", True),
])
def test_network_boundaries(host, allowed):
    assert scope_contains(Resource.NETWORK, "*.example.com", host) == allowed


def test_narrowed_filesystem_capability(tmp_path):
    parent = Capability(Resource.FILESYSTEM, frozenset({"read", "write"}), str(tmp_path), "k", "p")
    child = Capability(Resource.FILESYSTEM, frozenset({"read"}), str(tmp_path / "child"), "p", "c")
    assert child.is_subset_of(parent)
    assert not parent.is_subset_of(child)
