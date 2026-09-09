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
    adapter = CodexExecutor(workspaces, authority)
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
