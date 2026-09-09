from dataclasses import replace

import pytest

from praxis.kernel.candidates import Candidate, CandidateGroup, CandidateState


def test_group_roundtrip_and_transitions():
    group = CandidateGroup("parent", "common task", (Candidate("a", "p1"), Candidate("b", "p2")))
    assert CandidateGroup.from_json(group.to_json()) == group
    with pytest.raises(ValueError):
        group.move(CandidateState.SELECTED, "a")
    group = group.move(CandidateState.RUNNING).move(CandidateState.EVALUATING)
    with pytest.raises(ValueError):
        group.move(CandidateState.SELECTED, "unknown")
    selected = group.move(CandidateState.SELECTED, "a")
    assert CandidateGroup.from_json(selected.to_json()) == selected
    with pytest.raises(ValueError):
        selected.move(CandidateState.RUNNING)
    with pytest.raises(ValueError):
        replace(group, candidates=(Candidate("a", "p1"), Candidate("a", "p2")))


def test_parallel_candidate_isolation(tmp_path):
    import asyncio
    import sys

    from praxis.executors.local import LocalProcessExecutor
    from praxis.kernel.authority import Authority
    from praxis.kernel.capabilities import Resource
    from praxis.kernel.runtime import Kernel
    from praxis.kernel.spec import ProcessSpec
    from praxis.kernel.speculation import Speculation
    from praxis.storage.sqlite import SQLiteStore
    from praxis.workspaces.local import LocalWorkspaces
    from praxis.workspaces.transaction import CanonicalDirectory

    async def exercise():
        provider = LocalWorkspaces(tmp_path / "ws")
        authority = Authority(execution_defaults=frozenset({"local"}))
        canonical = CanonicalDirectory(tmp_path / "canonical")
        kernel = Kernel(SQLiteStore(tmp_path / "db"), provider, {"local": LocalProcessExecutor(provider)},
                        authority=authority)
        parent = kernel.create(ProcessSpec("compare", "local", budget={"wall_milliseconds": 10000}))
        speculation = Speculation(kernel)
        specs = tuple(ProcessSpec("variant", "local", inputs={"argv": [sys.executable, "-c",
            f"from pathlib import Path; Path('answer').write_text('{i}')"]},
            budget={"wall_milliseconds": 1000}) for i in range(2))
        group = speculation.fork(parent.process_id, "same objective", specs, canonical=canonical)
        for candidate in group.candidates:
            authority.issue(candidate.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}), str(canonical.root))
            authority.issue(candidate.process_id, Resource.WORKSPACE, frozenset({"commit"}), candidate.process_id)
        assert (await speculation.collect(group.group_id)).state == CandidateState.EVALUATING
        assert not (canonical.path / "answer").exists()
        paths = [provider.path_for(kernel.handles[c.process_id], c.process_id) for c in group.candidates]
        assert paths[0] != paths[1]
        assert [(path / "answer").read_text() for path in paths] == ["0", "1"]
        assert len(kernel.staged_transactions) == 2
        with pytest.raises(ValueError, match="budget"):
            speculation.fork(parent.process_id, "too much", (replace(specs[0], budget={"wall_milliseconds": 99999}),))
    asyncio.run(exercise())
