"""Per-host async token-bucket rate limiting + a global concurrency gate.

Politeness is enforced here rather than by `asyncio.sleep()` sprinkled through
the scrapers, so a single knob (`HOMZ_PER_HOST_RPS`) governs the whole fleet
and a `Retry-After` header can push a host into a temporary cool-down that
every coroutine respects.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from urllib.parse import urlsplit

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate = max(rate, 0.01)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def penalize(self, seconds: float) -> None:
        """Push this bucket into a cool-down (e.g. after a 429/503)."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()

                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    elapsed = now - self._updated
                    self._updated = now
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) / self.rate

            await asyncio.sleep(min(wait, 30.0))


class RateLimiter:
    """Keeps one bucket per host plus a global in-flight semaphore."""

    def __init__(
        self,
        *,
        per_host_rps: float | None = None,
        burst: int | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self.per_host_rps = per_host_rps if per_host_rps is not None else settings.per_host_rps
        self.burst = burst if burst is not None else settings.per_host_burst
        self._buckets: dict[str, TokenBucket] = {}
        self._overrides: dict[str, float] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrency)
        self._lock = asyncio.Lock()
        self._stats: dict[str, int] = defaultdict(int)

    def set_host_rate(self, host: str, rps: float) -> None:
        """Let a source declare a stricter limit than the global default."""
        self._overrides[host.lower()] = rps

    @staticmethod
    def host_of(url: str) -> str:
        return (urlsplit(url).netloc or url).lower()

    async def _bucket(self, host: str) -> TokenBucket:
        async with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                rate = self._overrides.get(host, self.per_host_rps)
                bucket = TokenBucket(rate=rate, burst=self.burst)
                self._buckets[host] = bucket
            return bucket

    async def penalize(self, url: str, seconds: float) -> None:
        host = self.host_of(url)
        bucket = await self._bucket(host)
        bucket.penalize(seconds)
        self._stats[f"penalty:{host}"] += 1
        log.warning("rate_limit.penalty", host=host, seconds=round(seconds, 1))

    def slot(self, url: str) -> _Slot:
        """`async with limiter.slot(url):` — acquires the host bucket and a
        global concurrency slot, releasing both on exit."""
        return _Slot(self, url)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class _Slot:
    def __init__(self, limiter: RateLimiter, url: str) -> None:
        self._limiter = limiter
        self._url = url

    async def __aenter__(self) -> None:
        await self._limiter._semaphore.acquire()
        try:
            bucket = await self._limiter._bucket(RateLimiter.host_of(self._url))
            await bucket.acquire()
        except BaseException:
            self._limiter._semaphore.release()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        self._limiter._semaphore.release()
