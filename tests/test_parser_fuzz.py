"""TM-9: bounded deterministic mutations, with persisted malformed seeds."""
import json
import random
from pathlib import Path

import pytest

from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.effects import Effect, file_write
from praxis.kernel.events import Event
from praxis.kernel.spec import ProcessSpec

MODELS = [ProcessSpec("task", "fake"), Event("p", "event"),
          Capability(Resource.SECRET, frozenset({"read"}), "secret", "kernel", "p"),
          file_write("p", "a", "/tmp/output", b"data")]


@pytest.mark.parametrize("model", MODELS, ids=["spec", "event", "capability", "effect"])
def test_deterministic_schema_mutations(model):
    randomizer = random.Random(132)
    base = json.loads(model.to_json())
    values = [None, True, False, 0, -1, 1.5, "", "\x00", [], {}, [None], {"nested": []}, "x" * 1000]
    corpus = json.loads((Path(__file__).parent / "fixtures/fuzz/regressions.json").read_text())
    corpus += ["[" * 1000 + "]" * 1000]
    for _ in range(500):
        mutation = dict(base)
        key = randomizer.choice(list(base))
        if randomizer.randrange(4) == 0:
            mutation.pop(key)
        else:
            mutation[key] = randomizer.choice(values)
        corpus.append(json.dumps(mutation))
    for raw in corpus:
        try:
            parsed = type(model).from_json(raw)
        except ValueError:
            continue
        assert type(model).from_json(parsed.to_json()) == parsed


def test_cycles_and_deep_input_rejected_before_recursion_crash():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        Event("p", "event", cyclic)
    with pytest.raises(ValueError):
        ProcessSpec("task", "fake", inputs=cyclic)


@pytest.mark.parametrize("model", [ProcessSpec, Event, Capability, Effect])
def test_duplicate_fields_never_select_a_policy(model):
    with pytest.raises(ValueError):
        model.from_json('{"schema_version":2,"schema_version":1}')
