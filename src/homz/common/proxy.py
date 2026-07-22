"""Proxy pool middleware.

Supports round-robin, random and sticky-per-host selection, with automatic
benching of proxies that fail or get blocked. If no proxies are configured the
pool is a no-op and everything goes out on the host's own IP — the pool is an
operational tool for spreading legitimate load and surviving flaky egress, not
a way to evade a site that has told us to stop.
"""

from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


@dataclass
class ProxyEntry:
    url: str
    failures: int = 0
    successes: int = 0
    benched_until: float = 0.0
    last_used: float = field(default=0.0)

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.benched_until

    @property
    def label(self) -> str:
        """Redacted form for logs — never log proxy credentials."""
        try:
            host = self.url.split("@")[-1]
        except Exception:  # pragma: no cover - defensive
            host = "?"
        return host


class ProxyPool:
    def __init__(
        self,
        proxies: list[str] | None = None,
        *,
        strategy: str | None = None,
        cooldown_seconds: int | None = None,
    ) -> None:
        urls = proxies if proxies is not None else settings.proxies
        self._entries: list[ProxyEntry] = [ProxyEntry(url=u) for u in urls if u]
        self._strategy = (strategy or settings.proxy_strategy).lower()
        self._cooldown = cooldown_seconds or settings.proxy_cooldown_seconds
        self._cycle = itertools.cycle(self._entries) if self._entries else None
        self._sticky: dict[str, ProxyEntry] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._entries)

    def _available(self) -> list[ProxyEntry]:
        return [e for e in self._entries if e.available]

    def acquire(self, host: str | None = None) -> ProxyEntry | None:
        if not self._entries:
            return None

        available = self._available()
        if not available:
            # Everything is benched — release the one that recovers soonest
            # rather than stalling the crawl entirely.
            entry = min(self._entries, key=lambda e: e.benched_until)
            entry.benched_until = 0.0
            log.warning("proxy.all_benched_releasing_earliest", proxy=entry.label)
            available = [entry]

        if self._strategy == "random":
            entry = random.choice(available)
        elif self._strategy == "sticky_per_host" and host:
            entry = self._sticky.get(host)
            if entry is None or not entry.available:
                entry = random.choice(available)
                self._sticky[host] = entry
        else:  # round_robin
            entry = available[0]
            if self._cycle is not None:
                for _ in range(len(self._entries)):
                    candidate = next(self._cycle)
                    if candidate.available:
                        entry = candidate
                        break

        entry.last_used = time.monotonic()
        return entry

    def report_success(self, entry: ProxyEntry | None) -> None:
        if entry is None:
            return
        entry.successes += 1
        entry.failures = 0

    def report_failure(self, entry: ProxyEntry | None, *, hard_block: bool = False) -> None:
        if entry is None:
            return
        entry.failures += 1
        if hard_block or entry.failures >= 3:
            entry.benched_until = time.monotonic() + self._cooldown
            log.warning(
                "proxy.benched",
                proxy=entry.label,
                failures=entry.failures,
                cooldown_s=self._cooldown,
                hard_block=hard_block,
            )

    def stats(self) -> list[dict[str, object]]:
        return [
            {
                "proxy": e.label,
                "successes": e.successes,
                "failures": e.failures,
                "available": e.available,
            }
            for e in self._entries
        ]
