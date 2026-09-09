from dataclasses import replace

import pytest

from praxis.kernel.capabilities import Resource
from praxis.kernel.effects import Effect, EffectAuthority, artifact_publish, file_write, git_commit, message_send


def test_concrete_effect_payloads_and_authority(tmp_path):
    effects = [file_write("p", "a", str(tmp_path / "file"), b"\x00\xff"),
               git_commit("p", "a", "repo", "change", "abc123"),
               message_send("p", "a", "channel", "message"),
               artifact_publish("p", "a", "registry", "sha256:abc", "application/octet-stream")]
    for effect in effects:
        assert Effect.from_json(effect.to_json()) == effect
        with pytest.raises(ValueError):
            replace(effect, authority=EffectAuthority(Resource.EXECUTOR, "execute", "local"))
    assert effects[0].reversible and not effects[2].reversible
    with pytest.raises(ValueError):
        replace(effects[2], reversible=True)
    assert not (tmp_path / "file").exists()
