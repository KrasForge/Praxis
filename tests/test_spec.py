import json

import pytest

from praxis.kernel.spec import ProcessSpec, SpecError


def test_roundtrip():
    spec = ProcessSpec("do work", "local", inputs={"argv": ["echo", "hello"]},
                       environment={"LANG": "C"}, capabilities=[{"type": "executor"}],
                       contract={"validators": []}, budget={"tokens": 100},
                       deadline="2026-09-10T00:00:00Z", metadata={"example.note": "test"})
    assert ProcessSpec.from_json(spec.to_json()) == spec
    assert ProcessSpec.from_json('{"objective":"work","executor":"local"}').inputs == {}


@pytest.mark.parametrize("changes", [
    {"objective": ""}, {"executor": None}, {"inputs": []},
    {"environment": {"X": 1}}, {"capabilities": ["all"]},
    {"budget": {"tokens": float("inf")}}, {"deadline": "tomorrow"},
    {"schema_version": True}, {"extra": 1},
])
def test_invalid(changes):
    data = {"objective": "work", "executor": "local", **changes}
    with pytest.raises(SpecError) as exc:
        ProcessSpec.from_json(json.dumps(data))
    assert exc.value.to_dict()["code"] == "invalid_process_spec"
