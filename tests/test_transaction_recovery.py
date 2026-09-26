"""Staged candidate transactions survive a controller restart, or are refused."""

import asyncio
import shutil
from pathlib import Path

import pytest

from praxis.evaluators.protocol import Evaluation, FakeEvaluator
from praxis.evaluators.selection import SelectionPolicy
from praxis.executors.fake import FakeExecutor
from praxis.executors.protocol import ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.speculation import Speculation
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory, WorkspaceTransaction


class WriterExecutor(FakeExecutor):
    """Writes inputs["answer"] into the workspace, as a real candidate would."""

    async def start(self, request: ExecutionRequest) -> ControlResult:
        (request.workspace_path / "answer").write_text(str(request.spec.inputs["answer"]))
        return await super().start(request)


def boot(root: Path) -> tuple[Kernel, SQLiteStore, Speculation]:
    store = SQLiteStore(root / "runtime.db")
    workspaces = LocalWorkspaces(root / "workspaces")
    kernel = Kernel(store, workspaces, {"writer": WriterExecutor()},
                    authority=Authority(execution_defaults=frozenset({"writer"})))
    return kernel, store, Speculation(kernel)


def restart(root: Path, store: SQLiteStore) -> tuple[Kernel, SQLiteStore, Speculation]:
    store.close()
    kernel, store, speculation = boot(root)
    kernel.recover_records()
    speculation.recover_groups()
    return kernel, store, speculation


async def staged_candidates(root: Path) -> tuple[Kernel, SQLiteStore, Speculation, CanonicalDirectory, str]:
    """Fork two candidates against a canonical store and collect them, uncommitted."""
    kernel, store, speculation = boot(root)
    canonical = CanonicalDirectory(root / "canonical")
    (canonical.path / "answer").write_text("baseline")
    parent = kernel.create(ProcessSpec("compare", "writer"))
    specs = tuple(ProcessSpec("variant", "writer", inputs={"answer": value}) for value in ("first", "second"))
    group = speculation.fork(parent.process_id, "answer", specs, canonical=canonical)
    for candidate in group.candidates:
        kernel.authority.issue(candidate.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}),
                               str(canonical.root))
        kernel.authority.issue(candidate.process_id, Resource.WORKSPACE, frozenset({"commit"}),
                               candidate.process_id)
    await speculation.collect(group.group_id)
    assert len(kernel.staged_transactions) == 2
    return kernel, store, speculation, canonical, group.group_id


async def select_first(speculation: Speculation, group_id: str) -> str:
    group = speculation.groups[group_id]
    ids = tuple(c.candidate_id for c in group.candidates)
    judge = FakeEvaluator(Evaluation("judge", "ok", ((ids[0], 2.0), (ids[1], 1.0))))
    selection = await speculation.select(group_id, SelectionPolicy("score", frozenset(ids), frozenset({"judge"})),
                                         (judge,), "{}")
    assert selection.winners == (ids[0],)
    return ids[0]


def events(kernel: Kernel, kind: str) -> list[dict[str, object]]:
    return [event.payload for event in kernel.events if event.type == kind]


def test_restart_between_staging_and_commit_publishes_the_verified_snapshot(tmp_path):
    async def exercise():
        kernel, store, speculation, canonical, group_id = await staged_candidates(tmp_path)
        staged = {event.process_id: event.payload for event in kernel.events if event.type == "transaction.staged"}
        assert len(staged) == 2

        kernel, store, speculation = restart(tmp_path, store)
        assert set(kernel.staged_transactions) == set(staged)
        assert len(events(kernel, "transaction.recovered")) == 2
        assert kernel.abandoned_transactions == {}
        assert (canonical.path / "answer").read_text() == "baseline"

        winner = await select_first(speculation, group_id)
        speculation.commit_selected(group_id, winner)
        process_id = next(c.process_id for c in speculation.groups[group_id].candidates if c.candidate_id == winner)
        committed = events(kernel, "workspace.committed")
        assert [event["snapshot_id"] for event in committed] == [staged[process_id]["snapshot_id"]]
        assert (CanonicalDirectory(tmp_path / "canonical").path / "answer").read_text() == "first"

        # On the next restart the winner stays closed, and the loser's baseline has moved.
        kernel, store, _ = restart(tmp_path, store)
        assert kernel.staged_transactions == {}
        loser = next(p for p in staged if p != process_id)
        assert kernel.abandoned_transactions == {loser: "stale_baseline"}
        store.close()
    asyncio.run(exercise())


def test_moved_baseline_abandons_and_publishes_nothing(tmp_path):
    async def exercise():
        kernel, store, speculation, canonical, group_id = await staged_candidates(tmp_path)
        # Another writer publishes while the controller is down.
        other = LocalWorkspaces(tmp_path / "other")
        handle = other.create("elsewhere")
        transaction = WorkspaceTransaction(other, handle, canonical)
        (other.path_for(handle, "elsewhere") / "answer").write_text("elsewhere")
        from praxis.validators.policy import VerificationReport
        snapshot = other.snapshot(handle)
        transaction.commit(VerificationReport(snapshot.snapshot_id, True, (), (), (), (), True))

        kernel, store, speculation = restart(tmp_path, store)
        assert kernel.staged_transactions == {}
        assert {event["reason"] for event in events(kernel, "transaction.abandoned")} == {"stale_baseline"}
        winner = await select_first(speculation, group_id)
        with pytest.raises(ValueError, match="staged_transaction_abandoned"):
            speculation.commit_selected(group_id, winner)
        assert (canonical.path / "answer").read_text() == "elsewhere"
        assert events(kernel, "workspace.committed") == []

        # Abandonment is journaled once, and a later restart still refuses the commit.
        kernel, store, speculation = restart(tmp_path, store)
        assert len(events(kernel, "transaction.abandoned")) == 2
        assert set(kernel.abandoned_transactions.values()) == {"stale_baseline"}
        with pytest.raises(ValueError, match="staged_transaction_abandoned"):
            speculation.commit_selected(group_id, winner)
        assert (canonical.path / "answer").read_text() == "elsewhere"
        store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("damage, reason", [
    ("tamper", "snapshot_mismatch"),
    ("delete", "snapshot_missing"),
    ("baseline", "baseline_missing"),
])
def test_damaged_staged_workspace_is_rejected_on_recovery(tmp_path, damage, reason):
    async def exercise():
        kernel, store, speculation, canonical, group_id = await staged_candidates(tmp_path)
        first = speculation.groups[group_id].candidates[0].process_id
        record = next(e.payload for e in kernel.events if e.type == "transaction.staged" and e.process_id == first)
        workspace = tmp_path / "workspaces" / "data" / str(record["workspace_id"])
        if damage == "tamper":
            (workspace / "answer").write_text("forged")
        elif damage == "delete":
            shutil.rmtree(workspace)
        else:
            (tmp_path / "workspaces" / "snapshots" / str(record["baseline_snapshot_id"])).write_bytes(b"[]")

        kernel, store, speculation = restart(tmp_path, store)
        if damage == "baseline":
            # Both candidates started from the same canonical tree, so they share the manifest.
            assert set(kernel.abandoned_transactions.values()) == {reason} and kernel.staged_transactions == {}
        else:
            assert kernel.abandoned_transactions == {first: reason}
            assert first not in kernel.staged_transactions
        winner = await select_first(speculation, group_id)
        with pytest.raises(ValueError, match="staged_transaction_abandoned"):
            speculation.commit_selected(group_id, winner)
        assert (canonical.path / "answer").read_text() == "baseline"
        store.close()
    asyncio.run(exercise())


def test_missing_canonical_store_is_not_recreated(tmp_path):
    async def exercise():
        kernel, store, speculation, canonical, group_id = await staged_candidates(tmp_path)
        shutil.rmtree(canonical.root)
        kernel, store, _ = restart(tmp_path, store)
        assert set(kernel.abandoned_transactions.values()) == {"canonical_missing"}
        assert not canonical.root.exists()
        store.close()
    asyncio.run(exercise())
