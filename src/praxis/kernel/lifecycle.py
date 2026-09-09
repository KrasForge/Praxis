"""Explicit process lifecycle; terminal states cannot be resumed."""

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TRANSITIONS: Mapping[State, frozenset[State]] = MappingProxyType({
    State.PENDING: frozenset({State.RUNNING, State.FAILED, State.CANCELLED}),
    State.RUNNING: frozenset({State.SUSPENDED, State.COMPLETED, State.FAILED,
                              State.CANCELLED}),
    State.SUSPENDED: frozenset({State.RUNNING, State.FAILED, State.CANCELLED}),
    State.COMPLETED: frozenset(),
    State.FAILED: frozenset(),
    State.CANCELLED: frozenset(),
})
TERMINAL = frozenset({State.COMPLETED, State.FAILED, State.CANCELLED})


class TransitionError(ValueError):
    code = "invalid_transition"

    def __init__(self, source: State, target: State):
        self.source = source
        self.target = target
        super().__init__(f"{self.code}: {source.value} -> {target.value}")


def transition(source: State, target: State) -> State:
    if not isinstance(source, State) or not isinstance(target, State):
        raise TypeError("transition requires State values")
    if target not in TRANSITIONS[source]:
        raise TransitionError(source, target)
    return target
