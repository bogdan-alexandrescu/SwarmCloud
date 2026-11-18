"""Per-principal token bucket.

Deliberately in-process. A Firestore-backed limiter would add a read and a write
to every request to protect against a burst that the Cloud Run concurrency
setting already bounds, and it would make the limiter itself the hot spot. With
N instances the effective ceiling is N x `requests_per_second`, which is the
honest trade and is documented on the /v1/stats response.

Keyed by principal, not by IP: the point is to stop one caller monopolising the
API, and every caller is authenticated before this runs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .errors import RateLimited


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = 10_000,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate = float(rate_per_second)
        self._burst = float(burst)
        self._clock = clock
        self._max_tracked = max_tracked
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def _evict_if_needed(self, now: float) -> None:
        if len(self._buckets) <= self._max_tracked:
            return
        # Drop the buckets that have been idle longest and are already full;
        # a full bucket carries no state worth keeping.
        stale = [
            key
            for key, bucket in self._buckets.items()
            if bucket.tokens >= self._burst and now - bucket.updated_at > 60
        ]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) > self._max_tracked:
            oldest = sorted(self._buckets.items(), key=lambda kv: kv[1].updated_at)
            for key, _ in oldest[: len(self._buckets) - self._max_tracked]:
                self._buckets.pop(key, None)

    def check(self, key: str, cost: float = 1.0) -> None:
        """Consume `cost` tokens or raise RateLimited with a real retry delay."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst, updated_at=now)
                self._buckets[key] = bucket
                self._evict_if_needed(now)
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now
            if bucket.tokens < cost:
                deficit = cost - bucket.tokens
                raise RateLimited(
                    "rate limit exceeded for this principal",
                    retry_after_seconds=deficit / self._rate,
                )
            bucket.tokens -= cost

    def snapshot(self, key: str) -> float:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self._burst
            elapsed = max(0.0, self._clock() - bucket.updated_at)
            return min(self._burst, bucket.tokens + elapsed * self._rate)
