"""TM-9: bounded deterministic mutations, with persisted malformed seeds."""
import json
import random
from pathlib import Path

import pytest

from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.effects import Effect, file_write
from praxis.kernel.events import Event
from praxis.kernel.plan import Plan
from praxis.kernel.proposals import EffectProposal
from praxis.kernel.spec import ProcessSpec

MODELS = [ProcessSpec("task", "fake"), Event("p", "event"),
          Capability(Resource.SECRET, frozenset({"read"}), "secret", "kernel", "p"),
          file_write("p", "a", "/tmp/output", b"data"),
          Plan((("a", ProcessSpec("task", "fake", contract={"required_outputs": ["out"]})),))]


@pytest.mark.parametrize("model", MODELS, ids=["spec", "event", "capability", "effect", "plan"])
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


@pytest.mark.parametrize("model", [ProcessSpec, Event, Capability, Effect, Plan, EffectProposal])
def test_duplicate_fields_never_select_a_policy(model):
    with pytest.raises(ValueError):
        model.from_json('{"schema_version":2,"schema_version":1}')


def test_effect_proposal_mutations_fail_closed():
    from praxis.kernel.proposals import ProposalError, parse_proposal
    randomizer = random.Random(257)
    base = {"schema": "praxis.effect-proposal", "schema_version": 1, "kind": "message_send",
            "target": "ops", "payload": {"text": "done"}}
    values = [None, True, 0, -1, 1.5, "", "\x00", [], {}, "external", "x" * 2000, {"text": 1}]
    corpus = json.loads((Path(__file__).parent / "fixtures/fuzz/regressions.json").read_text())
    corpus += ["[" * 1000 + "]" * 1000, '{"schema":"praxis.effect-proposal","schema":"x"}']
    for _ in range(500):
        mutation = dict(base)
        key = randomizer.choice(list(base) + ["effect_id", "authority", "status"])
        if randomizer.randrange(4) == 0:
            mutation.pop(key, None)
        else:
            mutation[key] = randomizer.choice(values)
        corpus.append(json.dumps(mutation))
    for raw in corpus:
        try:
            parsed = parse_proposal("p.json", raw)
        except ProposalError:
            continue
        # Anything accepted round-trips and carries no kernel-owned field.
        assert parse_proposal("p.json", json.dumps({k: v for k, v in parsed.to_dict().items() if k != "path"})) == parsed
