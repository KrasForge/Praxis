from dataclasses import replace

import pytest

from praxis.kernel.capabilities import Resource
from praxis.kernel.effects import Effect, EffectAuthority, EffectKind, EffectStatus


def effect():
    return Effect("p", "a", EffectKind.EXTERNAL, "target", {"data": [1]}, False,
                  EffectAuthority(Resource.EFFECT, "apply", "target"))


def test_effect_roundtrip():
    original = effect()
    assert Effect.from_json(original.to_json()) == original
    applied = original.move(EffectStatus.APPROVED).move(EffectStatus.APPLYING).move(EffectStatus.APPLIED)
    assert applied.version == 3 and applied.effect_id == original.effect_id


@pytest.mark.parametrize("source", list(EffectStatus))
@pytest.mark.parametrize("target", list(EffectStatus))
def test_all_effect_transition_edges(source, target):
    legal = {("proposed", "approved"), ("proposed", "rejected"), ("approved", "applying"),
             ("approved", "rejected"), ("applying", "applied"), ("applying", "failed")}
    current = replace(effect(), status=source)
    if (source.value, target.value) in legal:
        assert current.move(target).status == target
    else:
        with pytest.raises(ValueError):
            current.move(target)
