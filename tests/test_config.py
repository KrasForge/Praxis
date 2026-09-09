import json

import pytest

from praxis.kernel.config import Config, ConfigError, load_config


def test_precedence(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('max_concurrency = 2\nlog_level = "DEBUG"\n')
    assert load_config(environ={}) == Config()
    assert load_config(path, {}).max_concurrency == 2
    env = {"PRAXIS_MAX_CONCURRENCY": "3"}
    assert load_config(path, env).max_concurrency == 3
    assert load_config(path, env, {"max_concurrency": 5}).max_concurrency == 5
    assert json.loads(load_config(path, env).to_json())["log_level"] == "DEBUG"


@pytest.mark.parametrize("values", [
    {"max_concurrency": 0}, {"max_concurrency": True},
    {"storage_path": ""}, {"log_level": "bogus"}, {"secret": "canary"},
])
def test_invalid(values):
    with pytest.raises(ConfigError) as exc:
        load_config(environ={}, overrides=values)
    assert exc.value.to_dict()["code"] == "invalid_config"
    assert "canary" not in str(exc.value)


def test_bad_environment_and_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(environ={"PRAXIS_MAX_CONCURRENCY": "canary"})
    path = tmp_path / "bad.toml"
    path.write_text("invalid [")
    with pytest.raises(ConfigError):
        load_config(path, {})
