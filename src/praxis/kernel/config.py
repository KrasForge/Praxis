"""Strict TOML configuration with deterministic precedence."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    code = "invalid_config"

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{self.code}: {field}: {reason}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "reason": self.reason}


@dataclass(frozen=True)
class Config:
    storage_path: str = "praxis.db"
    workspace_root: str = ".praxis/workspaces"
    log_level: str = "INFO"
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        for key in ("storage_path", "workspace_root"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ConfigError(key, "expected nonempty path")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError("log_level", "unsupported log level")
        if type(self.max_concurrency) is not int or self.max_concurrency < 1:
            raise ConfigError("max_concurrency", "expected positive integer")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def load_config(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    values = asdict(Config())
    if path is not None:
        try:
            with path.open("rb") as stream:
                file_values = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise ConfigError("file", "cannot read valid TOML") from exc
        _merge(values, file_values)
    env = os.environ if environ is None else environ
    for key in values:
        name = f"PRAXIS_{key.upper()}"
        if name in env:
            value: Any = env[name]
            if key == "max_concurrency":
                try:
                    value = int(value)
                except ValueError:
                    raise ConfigError(key, "expected positive integer") from None
            values[key] = value
    _merge(values, overrides or {})
    return Config(**values)


def _merge(values: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key, value in incoming.items():
        if key not in values:
            raise ConfigError("schema", "unknown configuration field")
        values[key] = value
