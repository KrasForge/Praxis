import os

import pytest

from praxis.kernel.contracts import Contract
from praxis.validators.policy import evaluate
from praxis.validators.protocol import ValidationInput
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError
from praxis.workspaces.transaction import CanonicalDirectory, WorkspaceTransaction


def fixture(tmp_path):
    canonical = CanonicalDirectory(tmp_path / "canonical")
    (canonical.path / "file").write_bytes(b"original")
    provider = LocalWorkspaces(tmp_path / "ws")
    handle = provider.create("p")
    transaction = WorkspaceTransaction(provider, handle, canonical)
    (provider.path_for(handle, "p") / "file").write_bytes(b"changed")
    report = evaluate(Contract(), ValidationInput("p", "a", provider.snapshot(handle).snapshot_id, ()), ())
    return canonical, provider, handle, transaction, report


def test_external_conflict_and_rollback(tmp_path):
    canonical, provider, handle, transaction, report = fixture(tmp_path)
    (canonical.path / "file").write_bytes(b"external")
    with pytest.raises(WorkspaceError, match="conflict"):
        transaction.commit(report)
    transaction.rollback()
    assert (canonical.path / "file").read_bytes() == b"external"
    assert (provider.path_for(handle, "p") / "file").read_bytes() == b"original"


def test_interrupted_publication_keeps_old_revision(tmp_path, monkeypatch):
    canonical, provider, handle, transaction, report = fixture(tmp_path)
    def crash(*args):
        raise OSError("simulated crash before pointer publication")
    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        transaction.commit(report)
    assert (CanonicalDirectory(canonical.root).path / "file").read_bytes() == b"original"
    transaction.rollback()
    assert (provider.path_for(handle, "p") / "file").read_bytes() == b"original"


def test_competing_transaction_cannot_overwrite_winner(tmp_path):
    canonical, provider, handle, transaction, report = fixture(tmp_path)
    second_handle = provider.create("second")
    second = WorkspaceTransaction(provider, second_handle, canonical)
    transaction.commit(report)
    second_report = evaluate(Contract(), ValidationInput("second", "a", provider.snapshot(second_handle).snapshot_id, ()), ())
    with pytest.raises(WorkspaceError, match="conflict"):
        second.commit(second_report)
    with pytest.raises(WorkspaceError):
        transaction.rollback()
    assert (canonical.path / "file").read_bytes() == b"changed"
