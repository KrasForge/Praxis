import json
from dataclasses import replace

from praxis.executors.codex import CodexExecutor
from praxis.executors.protocol import ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


def test_codex_mapping(tmp_path):
    workspaces = LocalWorkspaces(tmp_path)
    authority = Authority()
    adapter = CodexExecutor(workspaces, authority, isolated_worker=True)
    request = ExecutionRequest("process", "attempt", ProcessSpec(
        objective="explain", executor="codex", inputs={"text": "a; $(bad)"},
        metadata={"codex": {"model": "example"}}), "workspace", tmp_path)
    mapped = adapter.map_request(request)
    argv = mapped.spec.inputs["argv"]
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert json.loads(mapped.spec.inputs["stdin"])["inputs"] == request.spec.inputs
    assert mapped.spec.metadata == request.spec.metadata
    assert request.spec.inputs == {"text": "a; $(bad)"}
    import pytest
    with pytest.raises(ValueError):
        adapter.map_request(replace(request, spec=replace(
            request.spec, metadata={"codex": {"sandbox": "danger-full-access"}})))


def test_codex_stream_fixtures(tmp_path):
    import asyncio

    from praxis.executors.outcomes import OutcomeStatus
    from praxis.kernel.capabilities import Resource

    async def exercise():
        provider = LocalWorkspaces(tmp_path / "workspaces")
        authority = Authority()
        authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "codex")
        for index, (events, expected) in enumerate([
            ([{"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
              {"type": "turn.completed"}], OutcomeStatus.COMPLETED),
            ([{"type": "turn.failed"}], OutcomeStatus.FAILED),
            ([{"type": "thread.started"}], OutcomeStatus.PARTIAL),
            ([], OutcomeStatus.UNAVAILABLE),
        ]):
            executable = tmp_path / f"fixture{index}"
            executable.write_text("#!/bin/sh\ncat >/dev/null\n" + "\n".join(
                "printf '%s\\n' '" + json.dumps(event) + "'" for event in events))
            executable.chmod(0o700)
            handle = provider.create("p")
            adapter = CodexExecutor(provider, authority, str(executable), isolated_worker=True)
            request = ExecutionRequest("p", str(index), ProcessSpec("task", "codex"),
                                       handle.workspace_id, provider.path_for(handle, "p"))
            assert (await adapter.start(request)).applied
            result = await adapter.collect_result(str(index))
            assert result.status == expected
            assert len(adapter.events) == len(events)
            if expected == OutcomeStatus.COMPLETED:
                assert result.stdout == "answer"
    asyncio.run(exercise())


def test_codex_controls(tmp_path):
    import asyncio

    from praxis.executors.outcomes import OutcomeStatus
    from praxis.kernel.capabilities import Resource

    async def exercise():
        provider = LocalWorkspaces(tmp_path / "workspaces")
        authority = Authority()
        authority.issue("p", Resource.EXECUTOR, frozenset({"execute"}), "codex")
        executable = tmp_path / "codex-fixture"
        executable.write_text("#!/bin/sh\ncat >/dev/null\nsleep 60\n")
        executable.chmod(0o700)
        adapter = CodexExecutor(provider, authority, str(executable), isolated_worker=True)
        handle = provider.create("p")
        request = ExecutionRequest("p", "a", ProcessSpec("wait", "codex"),
                                   handle.workspace_id, provider.path_for(handle, "p"))
        assert not (await adapter.checkpoint("a")).control.supported
        assert not (await adapter.signal("a", "suspend")).supported
        assert "checkpoint" not in adapter.descriptor.features
        assert "restore" not in adapter.descriptor.features
        assert (await adapter.start(request)).applied
        await asyncio.sleep(0.02)
        assert (await adapter.cancel("a")).applied
        assert (await adapter.collect_result("a")).status == OutcomeStatus.CANCELLED
        assert not (await adapter.cancel("a")).applied
    asyncio.run(exercise())
