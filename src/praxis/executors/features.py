"""Machine-readable executor features and deterministic requirement matching."""

import json
from dataclasses import asdict, dataclass, field


FEATURES = frozenset({"checkpoint", "restore", "signal", "isolation", "streaming",
                      "resource_reporting", "suspend", "cancel"})


@dataclass(frozen=True)
class ExecutorFeatures:
    name: str
    features: frozenset[str] = field(default_factory=frozenset)
    protocol_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("executor name required")
        if not isinstance(self.features, frozenset) or not self.features <= FEATURES:
            raise ValueError("unknown executor feature")
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise ValueError("unsupported executor protocol")

    def matches(self, required: frozenset[str], protocol_version: int = 1) -> bool:
        return (
            type(protocol_version) is int
            and protocol_version == self.protocol_version
            and required <= self.features
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["features"] = sorted(self.features)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ExecutorFeatures":
        try:
            data = json.loads(raw)
            if not isinstance(data["features"], list):
                raise ValueError("features must be an array")
            data["features"] = frozenset(data["features"])
            return cls(**data)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid executor descriptor") from exc
