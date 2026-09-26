"""In-memory admission limits for the single-controller host.

Token buckets refill continuously and start full, so ``N`` per minute also permits a
burst of ``N``. Buckets are keyed by the acting identity (``<client>`` or
``<client>/<user>``) and, optionally, by the client as a whole.
"""

import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from praxis.host.config import LimitsConfig


@dataclass
class Bucket:
    tokens: float
    updated: float


class TokenBuckets:
    def __init__(self, per_minute: int, clock: Callable[[], float]):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self.clock = clock
        self.buckets: dict[str, Bucket] = {}

    def take(self, key: str) -> float:
        """Consume one token; return 0 when admitted, else seconds until one is available."""
        now = self.clock()
        bucket = self.buckets.setdefault(key, Bucket(self.capacity, now))
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / self.rate


@dataclass(frozen=True)
class Rejection:
    status: int
    code: str
    retry_after: int | None = None


class Limiter:
    def __init__(self, config: LimitsConfig, clock: Callable[[], float] = time.monotonic):
        self.config = config

        def buckets(per_minute: int | None) -> TokenBuckets | None:
            return None if per_minute is None else TokenBuckets(per_minute, clock)
        self.requests = buckets(config.requests_per_minute)
        self.client_requests = buckets(config.client_requests_per_minute)
        self.submissions = buckets(config.submit_per_minute)
        self.streams: Counter[str] = Counter()
        self.rejections: Counter[str] = Counter()

    def admit(self, identity: str, client: str, *, submit: bool) -> Rejection | None:
        checks = [(self.requests, identity), (self.client_requests, client)]
        if submit:
            checks.append((self.submissions, identity))
        for buckets, key in checks:
            if buckets is not None:
                wait = buckets.take(key)
                if wait > 0:
                    self.rejections["rate_limited"] += 1
                    return Rejection(429, "rate_limited", max(1, math.ceil(wait)))
        return None

    def open_stream(self, identity: str) -> Rejection | None:
        limit = self.config.max_streams
        if limit is not None and self.streams[identity] >= limit:
            self.rejections["stream_limited"] += 1
            return Rejection(429, "stream_limited", 1)
        self.streams[identity] += 1
        return None

    def close_stream(self, identity: str) -> None:
        self.streams[identity] -= 1
        if self.streams[identity] <= 0:
            del self.streams[identity]
