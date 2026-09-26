"""Command-line entry point for the execution runtime."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from praxis import __version__


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["retention"]:
        return retention(args[1:])
    parser = argparse.ArgumentParser(prog="praxis", description="Praxis execution runtime",
                                     epilog="commands: retention (bounded disk use; see praxis retention --help)")
    parser.add_argument("--version", action="version", version=f"praxis {__version__}")
    parser.parse_args(args)
    return 0


def retention(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="praxis retention",
        description="Remove expired workspaces, snapshots and canonical revisions. "
                    "Reports only, unless --apply is given.")
    parser.add_argument("--db", type=Path, required=True, help="SQLite runtime database")
    parser.add_argument("--workspaces", type=Path, required=True, help="LocalWorkspaces root")
    parser.add_argument("--canonical", type=Path, action="append", default=[],
                        help="canonical directory to prune (repeatable)")
    parser.add_argument("--workspace-days", type=int, help="keep terminal workspaces and unreferenced snapshots this long")
    parser.add_argument("--canonical-revisions", type=int,
                        help="superseded canonical revisions to keep besides the current one")
    parser.add_argument("--apply", action="store_true", help="remove and journal; without it nothing changes")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    options = parser.parse_args(argv)

    from praxis.storage.sqlite import SQLiteStore
    from praxis.workspaces.local import LocalWorkspaces
    from praxis.workspaces.retention import RetentionError, RetentionPolicy, sweep

    try:
        policy = RetentionPolicy(options.workspace_days, options.canonical_revisions)
    except RetentionError as exc:
        parser.error(str(exc))
    if not options.db.is_file() or not options.workspaces.is_dir():
        parser.error("--db and --workspaces must name an existing database and workspace root")
    store = SQLiteStore(options.db)
    try:
        report = sweep(store, LocalWorkspaces(options.workspaces), policy, canonical=options.canonical,
                       dry_run=not options.apply)
    finally:
        store.close()
    if options.json:
        print(report.to_json())
        return 0
    verb = "removed" if options.apply else "would remove"
    counts: dict[str, int] = {}
    for item in report.removed:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    summary = ", ".join(f"{count} {kind}(s)" for kind, count in sorted(counts.items())) or "nothing"
    print(f"{verb} {summary}, {report.removed_bytes} bytes")
    kept: dict[str, int] = {}
    for item in report.kept:
        kept[item.reason] = kept.get(item.reason, 0) + 1
    for reason, count in sorted(kept.items()):
        print(f"kept {count}: {reason}")
    return 0
