"""Bounded retry policy; replay authority remains a kernel decision."""

import math
from dataclasses import dataclass


class RetryError(ValueError):
    code = "retry_denied"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0
    retryable_reasons: frozenset[str] = frozenset({"executor_error", "executor_unavailable", "deadline_exceeded"})

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("invalid attempt limit")
        if type(self.backoff_seconds) not in (int, float) or not math.isfinite(self.backoff_seconds) or self.backoff_seconds < 0:
            raise ValueError("invalid retry backoff")
