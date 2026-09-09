"""All advertised wire pairs plus historical optional-field and store readers."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from praxis.compatibility import CompatibilityError, VERSIONS, negotiate
from praxis.executors.features import ExecutorFeatures
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.contracts import Contract
from praxis.kernel.effects import file_write
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.remote.workers import Worker
from praxis.storage.memory import MemoryStore
from praxis.workspaces.local import LocalWorkspaces

MODELS = {
    "process_spec": ProcessSpec("task", "fake"),
    "process_result": ProcessResult("p", "a", State.FAILED, Outcome(OutcomeStatus.FAILED, "failed")),
    "event": Event("p", "event"), "executor": ExecutorFeatures("fake"),
    "capability": Capability(Resource.SECRET, frozenset({"read"}), "secret", "kernel", "p"),
    "contract": Contract(), "effect": file_write("p", "a", "/tmp/result", b"result"),
    "worker": Worker("w", "owner", "boot"),
}


@pytest.mark.parametrize("component", list(VERSIONS))
@pytest.mark.parametrize("writer", [1, 2])
def test_reader_writer_pairs(component, writer, tmp_path):
    if writer != 1:
        with pytest.raises(CompatibilityError, match="incompatible"):
            negotiate(component, [writer])
    if component == "workspace":
        provider = LocalWorkspaces(tmp_path)
        provider.protocol_version = writer
        if writer == 1:
            Kernel(MemoryStore(), provider, {})
        else:
            with pytest.raises(CompatibilityError):
                Kernel(MemoryStore(), provider, {})
        return
    model = MODELS[component]
    field = "protocol_version" if component in {"executor", "worker"} else "schema_version"
    if writer == 1:
        assert type(model).from_json(model.to_json()) == model
    else:
        data = json.loads(model.to_json())
        data[field] = writer
        with pytest.raises(ValueError):
            type(model).from_json(json.dumps(data))
        with pytest.raises(ValueError):
            replace(model, **{field: writer})


def test_historical_v1_optional_fields():
    fixture = json.loads((Path(__file__).parent / "fixtures/compatibility/legacy-v1.json").read_text())
    spec = ProcessSpec.from_json(json.dumps(fixture["process_spec"]))
    result = ProcessResult.from_json(json.dumps(fixture["process_result"]))
    assert spec.context == []
    assert result.error.code == "executor_failed"
    assert ProcessResult.from_json(result.to_json()) == result
