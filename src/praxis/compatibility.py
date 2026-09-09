"""Supported wire versions, independent of package and database versions."""

from types import MappingProxyType

VERSIONS = MappingProxyType({name: frozenset({1}) for name in (
    "process_spec", "process_result", "event", "executor", "workspace", "capability", "contract", "effect", "worker")})


class CompatibilityError(ValueError):
    code = "incompatible_version"


def negotiate(component: str, offered: list[int] | tuple[int, ...]) -> int:
    if component not in VERSIONS or not isinstance(offered, (list, tuple)) or not offered or any(type(v) is not int or v < 1 for v in offered):
        raise CompatibilityError("invalid_version_offer")
    shared = VERSIONS[component].intersection(offered)
    if not shared:
        raise CompatibilityError("incompatible_version")
    return max(shared)
