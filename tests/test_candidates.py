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
        from praxis.evaluators.protocol import Evaluation, FakeEvaluator
        from praxis.evaluators.selection import SelectionPolicy
        ids = tuple(c.candidate_id for c in group.candidates)
        policy = SelectionPolicy("score", frozenset(ids), frozenset({"judge"}))
        tied = FakeEvaluator(Evaluation("judge", "ok", tuple((identity, 1.0) for identity in ids)))
        assert (await speculation.select(group.group_id, policy, (tied,), '{"quality":"correct"}')).status == "tie"
        with pytest.raises(ValueError, match="not_selected"):
            speculation.commit_selected(group.group_id, ids[0])
        winner = FakeEvaluator(Evaluation("judge", "ok", ((ids[0], 2.0), (ids[1], 1.0))))
        assert (await speculation.select(group.group_id, policy, (winner,), '{}')).winners == (ids[0],)
        with pytest.raises(ValueError, match="not_selected"):
            speculation.commit_selected(group.group_id, ids[1])
        speculation.commit_selected(group.group_id, ids[0])
        assert (canonical.path / "answer").read_text() == "0"
        before = kernel.result(group.candidates[1].process_id).to_json()
        await speculation.cancel_losers(group.group_id)
        assert kernel.result(group.candidates[1].process_id).to_json() == before
        assert len([e for e in kernel.events if e.type == "candidate.evaluated"]) == 2
        with pytest.raises(ValueError, match="budget"):
            speculation.fork(parent.process_id, "too much", (replace(specs[0], budget={"wall_milliseconds": 99999}),))
    asyncio.run(exercise())


def test_select_completed_branch_and_cancel_running_loser(tmp_path):
    import asyncio
    import sys

    from praxis.evaluators.selection import SelectionPolicy
    from praxis.executors.local import LocalProcessExecutor
    from praxis.kernel.authority import Authority
    from praxis.kernel.lifecycle import State
    from praxis.kernel.runtime import Kernel
    from praxis.kernel.spec import ProcessSpec
    from praxis.kernel.speculation import Speculation
    from praxis.storage.memory import MemoryStore
    from praxis.workspaces.local import LocalWorkspaces

    async def exercise():
        provider = LocalWorkspaces(tmp_path)
        kernel = Kernel(MemoryStore(), provider, {"local": LocalProcessExecutor(provider)},
                        authority=Authority(execution_defaults=frozenset({"local"})))
        parent = kernel.create(ProcessSpec("compare", "local"))
        speculation = Speculation(kernel)
        group = speculation.fork(parent.process_id, "same task", tuple(
            ProcessSpec("variant", "local", inputs={"argv": [sys.executable, "-c", code]})
            for code in ["pass", "raise SystemExit(1)", "import time; time.sleep(60)"]))
        await kernel.tasks[group.candidates[0].process_id]
        await kernel.tasks[group.candidates[1].process_id]
        policy = SelectionPolicy("human", frozenset(c.candidate_id for c in group.candidates), frozenset())
        winner = group.candidates[0].candidate_id
        assert (await speculation.select(group.group_id, policy, (), '{}', human_choice=winner, actor="reviewer")).status == "selected"
        await speculation.cancel_losers(group.group_id)
        assert kernel.processes[group.candidates[1].process_id].state == State.FAILED
        assert kernel.processes[group.candidates[2].process_id].state == State.CANCELLED
        assert kernel.result(group.candidates[1].process_id).outcome.reason == "exit_nonzero"
    asyncio.run(exercise())
