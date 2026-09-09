import pytest

from praxis.kernel.contracts import Contract
from praxis.validators.policy import evaluate
from praxis.validators.protocol import ValidationInput
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError
from praxis.workspaces.transaction import CanonicalDirectory, WorkspaceTransaction


def test_staged_verified_atomic_publication(tmp_path):
    canonical = CanonicalDirectory(tmp_path / "canonical")
    (canonical.path / "file").write_text("old")
    old_path = canonical.path
    provider = LocalWorkspaces(tmp_path / "workspaces")
    handle = provider.create("p")
    transaction = WorkspaceTransaction(provider, handle, canonical)
    staged = provider.path_for(handle, "p")
    (staged / "file").write_text("new")
    assert (canonical.path / "file").read_text() == "old"
    snapshot = provider.snapshot(handle)
    source = ValidationInput("p", "a", snapshot.snapshot_id, (("file", b"new"),))
    denied = evaluate(Contract(required_outputs=("missing",)), source, ())
    with pytest.raises(WorkspaceError):
        transaction.commit(denied)
    event = transaction.commit(evaluate(Contract(), source, ()))
    assert (canonical.path / "file").read_text() == "new"
    assert (old_path / "file").read_text() == "old"
    assert event == transaction.receipt()
    assert event.type == "workspace.committed"
    assert (CanonicalDirectory(canonical.root).path / "file").read_text() == "new"


def test_stale_verification_cannot_commit(tmp_path):
    provider = LocalWorkspaces(tmp_path / "ws")
    handle = provider.create("p")
    transaction = WorkspaceTransaction(provider, handle, CanonicalDirectory(tmp_path / "canonical"))
    snapshot = provider.snapshot(handle)
    report = evaluate(Contract(), ValidationInput("p", "a", snapshot.snapshot_id, ()), ())
    (provider.path_for(handle, "p") / "new").write_text("unverified")
    with pytest.raises(WorkspaceError):
        transaction.commit(report)
