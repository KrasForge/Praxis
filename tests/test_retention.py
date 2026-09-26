"""Retention removes only what no recovery path can still need."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from praxis.evaluators.protocol import Evaluation, FakeEvaluator
from praxis.evaluators.selection import SelectionPolicy
from praxis.executors.fake import FakeExecutor
from praxis.executors.protocol import Checkpoint, ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.events import Event
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.speculation import Speculation
from praxis.storage.protocol import StoredCheckpoint
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.retention import DAY_SECONDS, RetentionError, RetentionPolicy, sweep
from praxis.workspaces.transaction import CanonicalDirectory, WorkspaceTransaction

LATER = time.time() + 30 * DAY_SECONDS  # far enough that everything made now has expired


class WriterExecutor(FakeExecutor):
    async def start(self, request: ExecutionRequest) -> ControlResult:
        (request.workspace_path / "answer").write_text(str(request.spec.inputs.get("answer", "x")))
        return await super().start(request)


def boot(root: Path) -> tuple[Kernel, SQLiteStore, LocalWorkspaces]:
    store = SQLiteStore(root / "runtime.db")
    workspaces = LocalWorkspaces(root / "workspaces")
    kernel = Kernel(store, workspaces, {"writer": WriterExecutor()}, retain_workspaces=True,
                    authority=Authority(execution_defaults=frozenset({"writer"})))
    return kernel, store, workspaces


async def run(kernel: Kernel, answer: str = "x") -> str:
    process = kernel.create(ProcessSpec("t", "writer", inputs={"answer": answer}))
    kernel.start(process.process_id)
    await kernel.tasks[process.process_id]
    return process.process_id


def kinds(report, reason=None):
    return sorted(item.kind for item in report.removed if reason is None or item.reason == reason)


def kept(report, reason):
    return [item for item in report.kept if item.reason == reason]


def test_policy_validation():
    with pytest.raises(RetentionError):
        RetentionPolicy(workspace_days=-1)
    with pytest.raises(RetentionError):
        RetentionPolicy(canonical_revisions=True)
    assert RetentionPolicy() == RetentionPolicy(None, None)


def test_dry_run_changes_nothing_and_apply_removes_and_journals(tmp_path):
    async def exercise():
        kernel, store, workspaces = boot(tmp_path)
        done = await run(kernel)
        workspace_id = kernel.handles[done].workspace_id
        policy = RetentionPolicy(workspace_days=7)

        # Still within the window: nothing goes.
        fresh = sweep(store, workspaces, policy, dry_run=False, journal=kernel.events)
        assert [item.identity for item in kept(fresh, "within_retention")] == [workspace_id]
        assert fresh.removed == ()

        before = sorted(p.relative_to(tmp_path) for p in (tmp_path / "workspaces").rglob("*"))
        preview = sweep(store, workspaces, policy, now=LATER)
        assert preview.dry_run and "workspace" in kinds(preview)
        assert sorted(p.relative_to(tmp_path) for p in (tmp_path / "workspaces").rglob("*")) == before
        assert not any(e.type == "retention.removed" for e in kernel.events)

        applied = sweep(store, workspaces, policy, now=LATER, dry_run=False, journal=kernel.events)
        assert [item.identity for item in applied.removed if item.kind == "workspace"] == [workspace_id]
        assert not (tmp_path / "workspaces" / "data" / workspace_id).exists()
        assert not (tmp_path / "workspaces" / "records" / f"{workspace_id}.json").exists()
        journaled = [e for e in store.read_events(done) if e.event.type == "retention.removed"]
        assert [e.event.payload["identity"] for e in journaled] == [workspace_id]
        assert [e for e in kernel.events if e.type == "retention.removed"]
        # Each applied sweep leaves an audit report; the dry run leaves none.
        audits = sorted((tmp_path / "workspaces" / "retention").glob("*.json"), key=lambda p: int(p.stem))
        assert len(audits) == 2
        latest = json.loads(audits[-1].read_text())
        assert latest["dry_run"] is False and workspace_id in {item["identity"] for item in latest["removed"]}
        # Every blob and manifest the finished run made was unreferenced and expired.
        assert not any((tmp_path / "workspaces" / "blobs").iterdir())
        assert kernel.result(done).state.value == "completed"  # the record itself is untouched
        store.close()
    asyncio.run(exercise())


def test_keeps_active_unowned_uncertain_and_staged_workspaces(tmp_path):
    async def exercise():
        kernel, store, workspaces = boot(tmp_path)
        canonical = CanonicalDirectory(tmp_path / "canonical")
        # A staged candidate that is still waiting for selection.
        parent = kernel.create(ProcessSpec("compare", "writer"))
        speculation = Speculation(kernel)
        group = speculation.fork(parent.process_id, "answer", (ProcessSpec("v", "writer", inputs={"answer": "a"}),),
                                 canonical=canonical)
        candidate = group.candidates[0].process_id
        kernel.authority.issue(candidate, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
        await speculation.collect(group.group_id)
        staged_workspace = kernel.handles[candidate].workspace_id
        # A finished process whose effect outcome is uncertain.
        uncertain = await run(kernel)
        kernel.events.append(Event(uncertain, "effect.applying", {"effect_id": "effect-1"}))
        # A workspace nobody in this store owns, and one for a process that has not finished.
        workspaces.create("someone-else")
        active = kernel.create(ProcessSpec("t", "writer")).process_id
        workspaces.create(active)

        report = sweep(store, workspaces, RetentionPolicy(workspace_days=0), now=LATER, dry_run=False,
                       journal=kernel.events)
        assert [i.identity for i in kept(report, "staged_transaction")] == [staged_workspace]
        assert [i.process_id for i in kept(report, "uncertain_effect")] == [uncertain]
        assert len(kept(report, "unowned")) == 1
        assert [i.process_id for i in kept(report, "process_active")] == [active]
        assert [i.kind for i in kept(report, "long_running_process")] == ["snapshot"]
        assert not [item for item in report.removed if item.kind in ("snapshot", "blob")]

        # The staged candidate still commits after the sweep and a restart.
        store.close()
        kernel, store, workspaces = boot(tmp_path)
        kernel.recover_records()
        speculation = Speculation(kernel)
        speculation.recover_groups()
        assert candidate in kernel.staged_transactions
        chosen = group.candidates[0].candidate_id
        kernel.authority.issue(candidate, Resource.WORKSPACE, frozenset({"commit"}), candidate)
        judge = FakeEvaluator(Evaluation("judge", "ok", ((chosen, 1.0),)))
        await speculation.select(group.group_id, SelectionPolicy("score", frozenset({chosen}), frozenset({"judge"})),
                                 (judge,), "{}")
        speculation.commit_selected(group.group_id, chosen)
        assert (canonical.path / "answer").read_text() == "a"
        store.close()
    asyncio.run(exercise())


def test_snapshots_referenced_by_staging_or_checkpoints_survive(tmp_path):
    async def exercise():
        kernel, store, workspaces = boot(tmp_path)
        done = await run(kernel, "checkpointed")
        snapshot = workspaces.snapshot(kernel.handles[done])
        process = kernel.processes[done]
        store.save_checkpoint(StoredCheckpoint(
            Checkpoint("writer", done, process.attempt_id, b"state", 1), snapshot.snapshot_id))
        # Remove the workspace so only the checkpoint references the snapshot.
        sweep(store, workspaces, RetentionPolicy(workspace_days=0), now=LATER, dry_run=False)
        manifests = {p.name for p in (tmp_path / "workspaces" / "snapshots").iterdir()}
        assert manifests == {snapshot.snapshot_id}
        # The checkpoint can still be restored into a new workspace.
        from praxis.kernel.checkpoints import CheckpointManager
        restore = CheckpointManager(store, workspaces, kernel.authority)
        handle = workspaces.create(done)
        restore.restore_workspace(handle, snapshot.snapshot_id)
        assert (workspaces.path_for(handle, done) / "answer").read_text() == "checkpointed"
        store.close()
    asyncio.run(exercise())


def test_canonical_revisions_keep_current_recent_and_staged_baselines(tmp_path):
    async def exercise():
        kernel, store, workspaces = boot(tmp_path)
        canonical = CanonicalDirectory(tmp_path / "canonical")
        other = LocalWorkspaces(tmp_path / "other")
        from praxis.validators.policy import VerificationReport

        def publish(text: str) -> str:
            handle = other.create("publisher")
            transaction = WorkspaceTransaction(other, handle, canonical)
            (other.path_for(handle, "publisher") / "answer").write_text(text)
            snapshot = other.snapshot(handle)
            return str(transaction.commit(VerificationReport(snapshot.snapshot_id, True, (), (), (), (), True))
                       .payload["revision"])

        initial = canonical.revision
        revisions = [publish(str(i)) for i in range(4)]
        # Pin the initial revision as the baseline of a pending staged transaction.
        pinned = kernel.create(ProcessSpec("t", "writer")).process_id
        kernel.events.append(Event(pinned, "transaction.staged", {
            "workspace_id": "w", "snapshot_id": "s", "canonical_root": str(canonical.root),
            "baseline_revision": initial, "baseline_snapshot_id": "b", "attempt_id": "a"}))

        report = sweep(store, workspaces, RetentionPolicy(canonical_revisions=1), canonical=[canonical.root],
                       dry_run=False, journal=kernel.events)
        remaining = {p.name for p in canonical.versions.iterdir()}
        assert remaining == {revisions[3], revisions[2], initial}
        assert canonical.revision == revisions[3]
        assert (canonical.path / "answer").read_text() == "3"
        assert {i.identity for i in report.removed} == {revisions[0], revisions[1]}
        assert [i.identity for i in kept(report, "staged_baseline")] == [initial]
        missing = sweep(store, workspaces, RetentionPolicy(canonical_revisions=1), canonical=[tmp_path / "nowhere"])
        assert kept(missing, "canonical_missing") and not (tmp_path / "nowhere").exists()
        store.close()
    asyncio.run(exercise())


def test_recovery_after_a_sweep(tmp_path):
    async def exercise():
        kernel, store, workspaces = boot(tmp_path)
        finished = [await run(kernel, str(i)) for i in range(3)]
        sweep(store, workspaces, RetentionPolicy(workspace_days=0), now=LATER, dry_run=False, journal=kernel.events)
        store.close()
        kernel, store, workspaces = boot(tmp_path)
        kernel.recover_records()
        assert [kernel.result(p).state.value for p in finished] == ["completed"] * 3
        assert (await run(kernel, "after")) in kernel.processes
        store.close()
    asyncio.run(exercise())


def test_cli_reports_and_applies(tmp_path):
    async def exercise():
        kernel, store, _ = boot(tmp_path)
        await run(kernel)
        store.close()
    asyncio.run(exercise())
    command = [sys.executable, "-m", "praxis", "retention", "--db", str(tmp_path / "runtime.db"),
               "--workspaces", str(tmp_path / "workspaces"), "--workspace-days", "0"]
    preview = subprocess.run(command, capture_output=True, text=True)
    assert preview.returncode == 0 and preview.stdout.startswith("would remove")
    assert any((tmp_path / "workspaces" / "data").iterdir())
    applied = subprocess.run([*command, "--apply", "--json"], capture_output=True, text=True)
    assert applied.returncode == 0
    report = json.loads(applied.stdout)
    assert report["dry_run"] is False and {item["kind"] for item in report["removed"]} >= {"workspace"}
    assert not any((tmp_path / "workspaces" / "data").iterdir())
    bad = subprocess.run([*command[:-1], "-1"], capture_output=True, text=True)
    assert bad.returncode == 2 and "workspace_days" in bad.stderr


def test_a_retry_that_starts_during_the_sweep_keeps_its_workspace(tmp_path):
    from praxis.executors.outcomes import Outcome, OutcomeStatus
    from praxis.kernel.retry import RetryPolicy

    async def exercise():
        store = SQLiteStore(tmp_path / "runtime.db")
        workspaces = LocalWorkspaces(tmp_path / "workspaces")
        failing = FakeExecutor(Outcome(OutcomeStatus.FAILED, "boom", retryable=True))
        kernel = Kernel(store, workspaces, {"writer": failing}, retain_workspaces=True,
                        authority=Authority(execution_defaults=frozenset({"writer"})))
        process = kernel.create(ProcessSpec("t", "writer"))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        workspace_id = kernel.handles[process.process_id].workspace_id
        stale = store.load(process.process_id)

        class OpeningSnapshot:
            """Serves the sweep's opening read from before the retry, and live reads after it."""

            def __init__(self):
                self.opened = False

            def __getattr__(self, name):
                return getattr(store, name)

            def load(self, identity):
                if not self.opened:
                    return stale
                return store.load(identity)

            def read_events(self, process_id=None, *, after=0):
                if process_id is None:
                    self.opened = True
                return store.read_events(process_id, after=after)

        await kernel.retry(process.process_id, RetryPolicy(max_attempts=3), start=False)
        report = sweep(OpeningSnapshot(), workspaces, RetentionPolicy(workspace_days=0), now=LATER, dry_run=False,
                       journal=kernel.events)
        assert [item.identity for item in kept(report, "process_active")] == [workspace_id]
        assert (tmp_path / "workspaces" / "data" / workspace_id).is_dir()
        store.close()
    asyncio.run(exercise())
