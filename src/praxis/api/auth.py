"""Deployment-supplied identity and authorization; anonymous access fails closed."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    identity: str
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("authenticated actor identity required")


@dataclass(frozen=True)
class SecurityHooks:
    authenticate: Callable[[dict[str, str]], Actor | None] = lambda headers: None
    authorize: Callable[[Actor, str, str | None], bool] = lambda actor, action, process: False
