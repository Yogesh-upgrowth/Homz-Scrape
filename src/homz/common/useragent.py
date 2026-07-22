"""User-Agent rotation.

Rotation here is about spreading load across plausible client identities, not
about impersonating a browser to defeat a bot check. The UA strings are real,
current desktop browsers; each one is paired with a matching `Sec-CH-UA` /
`Accept` header set so the header block is internally consistent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserAgentProfile:
    user_agent: str
    sec_ch_ua: str
    platform: str
    accept_language: str = "en-IN,en-GB;q=0.9,en;q=0.8,hi;q=0.7"
    extra: dict[str, str] = field(default_factory=dict)

    def headers(self) -> dict[str, str]:
        base = {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": self.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Connection": "keep-alive",
        }
        base.update(self.extra)
        return base


PROFILES: tuple[UserAgentProfile, ...] = (
    UserAgentProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        platform="Windows",
    ),
    UserAgentProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        platform="macOS",
    ),
    UserAgentProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
        ),
        sec_ch_ua='"Microsoft Edge";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        platform="Windows",
    ),
    UserAgentProfile(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        platform="Linux",
    ),
    UserAgentProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) "
            "Gecko/20100101 Firefox/132.0"
        ),
        sec_ch_ua="",
        platform="macOS",
        extra={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    ),
)


class UserAgentRotator:
    """Round-robin with a random start, so parallel workers don't sync up."""

    def __init__(self, profiles: tuple[UserAgentProfile, ...] = PROFILES) -> None:
        if not profiles:
            raise ValueError("at least one user-agent profile is required")
        self._profiles = profiles
        self._index = random.randrange(len(profiles))
        self._sticky: dict[str, UserAgentProfile] = {}

    def next(self) -> UserAgentProfile:
        profile = self._profiles[self._index % len(self._profiles)]
        self._index += 1
        return profile

    def for_host(self, host: str) -> UserAgentProfile:
        """Stable UA per host — flipping identity mid-session looks worse than
        keeping one, and it keeps cookie jars coherent."""
        if host not in self._sticky:
            self._sticky[host] = self.next()
        return self._sticky[host]

    def headers(self, host: str | None = None) -> dict[str, str]:
        profile = self.for_host(host) if host else self.next()
        headers = profile.headers()
        return {k: v for k, v in headers.items() if v}
