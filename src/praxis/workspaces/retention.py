"""Bounded disk use: remove terminal workspaces, old canonical revisions and unreferenced snapshots.

The sweep removes only state that no recovery path can still need. It never removes:

- a workspace whose process is not terminal, is unknown to the store, holds a staged
  transaction that is still pending, or has an effect whose outcome is uncertain;
- the current canonical revision, or the baseline revision of a pending staged
  transaction;
- a snapshot manifest that a pending staged transaction or a checkpoint references,
  or any blob that a kept manifest references.

It does not compact the journal. Process records and events are the audit trail and
the idempotency record for submissions, effects and publication; see ADR 0006.
"""

import fcntl
import json
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL
from praxis.kernel.process import Process
from praxis.storage.journal import EventJournal
from praxis.storage.protocol import ProcessStore, StoreError
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError, WorkspaceHandle
from praxis.workspaces.transaction import CanonicalDirectory, staged_transactions

DAY_SECONDS = 86_400


class RetentionError(ValueError):
    code = "invalid_retention_policy"


@dataclass(frozen=True)
class RetentionPolicy:
    """What to remove. A field left as None disables that part of the sweep."""

    workspace_days: int | None = None
    canonical_revisions: int | None = None

    def __post_init__(self) -> None:
        for name in ("workspace_days", "canonical_revisions"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 36_500):
                raise RetentionError(f"{name} must be an integer from 0 to 36500")


@dataclass(frozen=True)
class RetentionItem:
    kind: str  # workspace, revision, snapshot or blob
    identity: str
    reason: str  # why it was removed, or why it was kept
    process_id: str | None = None
    bytes: int = 0


@dataclass(frozen=True)
class RetentionReport:
    dry_run: bool
    swept_at: str
    removed: tuple[RetentionItem, ...]
    kept: tuple[RetentionItem, ...]

    @property
    def removed_bytes(self) -> int:
        return sum(item.bytes for item in self.removed)

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "removed_bytes": self.removed_bytes}, sort_keys=True)


def sweep(store: ProcessStore, workspaces: LocalWorkspaces, policy: RetentionPolicy, *,
          canonical: Iterable[Path] = (), dry_run: bool = True, now: float | None = None,
          journal: list[Event] | None = None) -> RetentionReport:
    """Remove what the policy allows and journal each removal owned by a process.

    With dry_run (the default) nothing changes and the report lists what would go.
    Pass the kernel's live journal as ``journal`` when a kernel is running in this
    process, so its in-memory event list sees the removals.
    """
    clock = time.time() if now is None else now
    sweeper = _Sweep(store, workspaces, policy, clock, dry_run)
    if policy.workspace_days is not None:
        sweeper.workspaces()
        sweeper.snapshots()
    if policy.canonical_revisions is not None:
        for root in canonical:
            sweeper.revisions(Path(root))
    report = RetentionReport(dry_run, datetime.fromtimestamp(clock, timezone.utc).isoformat(),
                             tuple(sweeper.removed), tuple(sweeper.kept))
    if not dry_run:
        sink = EventJournal(store) if journal is None else journal
        for item in report.removed:
            if item.process_id is not None and item.process_id in sweeper.processes:
                sink.append(Event(item.process_id, "retention.removed", {
                    "kind": item.kind, "identity": item.identity, "reason": item.reason, "bytes": item.bytes,
                }))
        audit = workspaces.root / "retention"
        audit.mkdir(exist_ok=True, mode=0o700)
        (audit / f"{int(clock * 1000)}.json").write_text(report.to_json())
    return report


def _epoch(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp).timestamp()


def _size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except OSError:
                pass
    return total


class _Sweep:
    def __init__(self, store: ProcessStore, workspaces: LocalWorkspaces, policy: RetentionPolicy,
                 clock: float, dry_run: bool):
        self.store = store
        self.provider = workspaces
        self.policy = policy
        self.clock = clock
        self.dry_run = dry_run
        self.removed: list[RetentionItem] = []
        self.kept: list[RetentionItem] = []
        self.processes: dict[str, Process] = {}
        for identity in store.list_processes():
            try:
                self.processes[identity] = store.load(identity)
            except StoreError:
                continue  # an unreadable record owns nothing the sweep may remove
        events = tuple(entry.event for entry in store.read_events())
        self.pending, _ = staged_transactions(events)
        self.created: dict[str, float] = {}
        self.terminal_at: dict[str, float] = {}
        last_effect: dict[str, tuple[str, str]] = {}
        for event in events:
            if event.type == "process.created":
                self.created[event.process_id] = _epoch(event.timestamp)
            elif event.type == "process.state" and event.payload.get("state") in {s.value for s in TERMINAL}:
                self.terminal_at[event.process_id] = _epoch(event.timestamp)
            elif event.type.startswith("effect.") and isinstance(event.payload.get("effect_id"), str):
                last_effect[event.payload["effect_id"]] = (event.process_id, event.type)
        self.uncertain = {process_id for process_id, kind in last_effect.values() if kind == "effect.applying"}

    def cutoff(self) -> float:
        assert self.policy.workspace_days is not None
        return self.clock - self.policy.workspace_days * DAY_SECONDS

    def workspaces(self) -> None:
        staged = {str(record.get("workspace_id")): process_id for process_id, record in self.pending.items()}
        for record_path in sorted(self.provider.records.glob("*.json")):
            workspace_id = record_path.stem
            try:
                owner = json.loads(record_path.read_text())["process_id"]
                if not isinstance(owner, str):
                    raise ValueError("owner")
            except (OSError, ValueError, KeyError, TypeError):
                self.kept.append(RetentionItem("workspace", workspace_id, "unreadable_record"))
                continue
            process = self.processes.get(owner)
            reason = None
            if process is None:
                reason = "unowned"
            elif process.state not in TERMINAL:
                reason = "process_active"
            elif workspace_id in staged:
                reason = "staged_transaction"
            elif owner in self.uncertain:
                reason = "uncertain_effect"
            elif self.terminal_at.get(owner, self.clock) > self.cutoff():
                reason = "within_retention"
            path = self.provider.data / workspace_id
            if reason is not None:
                self.kept.append(RetentionItem("workspace", workspace_id, reason, owner))
                continue
            item = RetentionItem("workspace", workspace_id, "terminal_expired", owner,
                                 _size(path) if path.exists() else 0)
            if not self.dry_run and not self.still_expired(owner):
                # The owner moved on since the sweep started, for example a retry began.
                self.kept.append(RetentionItem("workspace", workspace_id, "process_active", owner))
                continue
            if not self.dry_run:
                try:
                    self.provider.destroy(WorkspaceHandle(workspace_id, owner, "local"))
                except WorkspaceError:
                    # The directory is already gone; drop the orphaned record only.
                    if path.exists() or path.is_symlink():
                        self.kept.append(RetentionItem("workspace", workspace_id, "invalid_workspace", owner))
                        continue
                    record_path.unlink()
            self.removed.append(item)

    def still_expired(self, owner: str) -> bool:
        """Re-read the owner just before removal instead of trusting the opening snapshot."""
        try:
            process = self.store.load(owner)
        except StoreError:
            return False
        if process.state not in TERMINAL:
            return False
        last = None
        for entry in self.store.read_events(owner):
            if entry.event.type == "process.state":
                last = entry.event
        return (last is not None and last.payload.get("state") in {s.value for s in TERMINAL}
                and _epoch(last.timestamp) <= self.cutoff())

    def snapshots(self) -> None:
        cutoff = self.cutoff()
        # An in-memory rollback baseline of a live process is only as old as the process.
        if any(process.state not in TERMINAL and self.created.get(identity, self.clock) <= cutoff
               for identity, process in self.processes.items()):
            self.kept.append(RetentionItem("snapshot", "*", "long_running_process"))
            return
        referenced: set[str] = set()
        for record in self.pending.values():
            referenced |= {str(record.get("snapshot_id")), str(record.get("baseline_snapshot_id"))}
        for identity in self.processes:
            checkpoint = self.store.load_checkpoint(identity)
            if checkpoint is not None:
                referenced.add(checkpoint.workspace_snapshot_id)
        manifests = self.provider.root / "snapshots"
        blobs = self.provider.root / "blobs"
        if not manifests.is_dir():
            return
        with self.provider.snapshot_lock(exclusive=True):
            keep: list[Path] = []
            for manifest in sorted(manifests.iterdir()):
                if manifest.name in referenced or manifest.stat().st_mtime > cutoff:
                    keep.append(manifest)
                    continue
                self.removed.append(RetentionItem("snapshot", manifest.name, "unreferenced_expired",
                                                  bytes=manifest.stat().st_size))
                if not self.dry_run:
                    manifest.unlink()
            needed: set[str] = set()
            for manifest in keep:
                try:
                    needed |= {str(digest) for _, digest in json.loads(manifest.read_bytes())}
                except (OSError, ValueError, TypeError):
                    self.kept.append(RetentionItem("blob", "*", "unreadable_manifest"))
                    return
            if not blobs.is_dir():
                return
            for blob in sorted(blobs.iterdir()):
                if blob.name in needed or blob.stat().st_mtime > cutoff:
                    continue
                self.removed.append(RetentionItem("blob", blob.name, "unreferenced_expired",
                                                  bytes=blob.stat().st_size))
                if not self.dry_run:
                    blob.unlink()

    def revisions(self, root: Path) -> None:
        assert self.policy.canonical_revisions is not None
        try:
            canonical = CanonicalDirectory.open(root)
        except (WorkspaceError, OSError):
            self.kept.append(RetentionItem("revision", str(root), "canonical_missing"))
            return
        pinned = {str(record.get("baseline_revision")) for record in self.pending.values()
                  if record.get("canonical_root") == str(canonical.root)}
        with (canonical.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = canonical.revision
            dated: list[tuple[float, Path, str | None]] = []
            for version in canonical.versions.iterdir():
                if not version.is_dir() or version.is_symlink() or version.name == current:
                    continue
                receipt = version / "commit.json"
                committed, owner = version.stat().st_mtime, None
                if receipt.is_file():
                    try:
                        event = Event.from_json(receipt.read_text())
                        committed, owner = _epoch(event.timestamp), event.process_id
                    except ValueError:
                        pass
                dated.append((committed, version, owner))
            dated.sort(key=lambda entry: entry[0], reverse=True)
            for index, (_, version, owner) in enumerate(dated):
                if index < self.policy.canonical_revisions:
                    continue
                if version.name in pinned:
                    self.kept.append(RetentionItem("revision", version.name, "staged_baseline", owner))
                    continue
                self.removed.append(RetentionItem("revision", version.name, "superseded", owner, _size(version)))
                if not self.dry_run:
                    shutil.rmtree(version)
