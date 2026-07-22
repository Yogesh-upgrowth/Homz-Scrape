"""Block / captcha detection.

This module *detects* anti-bot walls so the crawler can stop and alert. It does
not attempt to solve, bypass or evade them — that is a deliberate design
decision (see COMPLIANCE.md). When a wall is detected the correct response is
to back off, reduce rate, and switch to a sanctioned data channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class BlockKind(StrEnum):
    NONE = "none"
    RATE_LIMITED = "rate_limited"
    CAPTCHA = "captcha"
    WAF = "waf"
    LOGIN_REQUIRED = "login_required"
    FORBIDDEN = "forbidden"
    EMPTY_SHELL = "empty_shell"


@dataclass(frozen=True)
class BlockSignal:
    kind: BlockKind
    reason: str
    retry_after: float | None = None

    @property
    def is_blocked(self) -> bool:
        return self.kind is not BlockKind.NONE

    @property
    def is_retryable(self) -> bool:
        """Rate limits ease off; captcha/WAF walls do not on their own."""
        return self.kind in {BlockKind.RATE_LIMITED, BlockKind.EMPTY_SHELL}


NOT_BLOCKED = BlockSignal(kind=BlockKind.NONE, reason="")

_CAPTCHA_MARKERS = (
    re.compile(r"g-recaptcha|recaptcha/api\.js|grecaptcha\.", re.I),
    re.compile(r"\bhcaptcha\b|hcaptcha\.com", re.I),
    re.compile(r"cf-turnstile|challenges\.cloudflare\.com", re.I),
    re.compile(r"px-captcha|_pxhd|perimeterx", re.I),
    re.compile(r"verify (you are|you're) (a )?human|are you a robot", re.I),
    re.compile(r"unusual traffic from your computer network", re.I),
)

_WAF_MARKERS = (
    re.compile(r"attention required!\s*\|\s*cloudflare", re.I),
    re.compile(r"access denied.*(akamai|reference\s*#)", re.I),
    re.compile(r"request unsuccessful\.\s*incapsula", re.I),
    re.compile(r"<title>\s*just a moment", re.I),
    re.compile(r"radware|distil|imperva", re.I),
    re.compile(r"blocked by (our )?security", re.I),
)

_LOGIN_MARKERS = (
    re.compile(r"please (log ?in|sign ?in) to (continue|view)", re.I),
    re.compile(r"session (has )?expired.*log ?in", re.I),
)


def detect_block(
    *,
    status_code: int,
    body: str | None,
    headers: dict[str, str] | None = None,
    min_body_chars: int = 800,
) -> BlockSignal:
    """Classify a response. Cheap string scan — safe to run on every fetch."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    retry_after = _parse_retry_after(headers.get("retry-after"))

    if status_code == 429:
        return BlockSignal(BlockKind.RATE_LIMITED, "HTTP 429", retry_after or 60.0)
    if status_code == 503:
        return BlockSignal(BlockKind.RATE_LIMITED, "HTTP 503", retry_after or 30.0)
    if status_code == 401:
        return BlockSignal(BlockKind.LOGIN_REQUIRED, "HTTP 401")

    sample = (body or "")[:200_000]

    if status_code == 403:
        for pattern in _CAPTCHA_MARKERS:
            if pattern.search(sample):
                return BlockSignal(BlockKind.CAPTCHA, f"403 + {pattern.pattern[:40]}")
        return BlockSignal(BlockKind.FORBIDDEN, "HTTP 403")

    if not sample.strip():
        return BlockSignal(BlockKind.EMPTY_SHELL, "empty response body")

    for pattern in _CAPTCHA_MARKERS:
        if pattern.search(sample):
            return BlockSignal(BlockKind.CAPTCHA, f"captcha marker: {pattern.pattern[:40]}")
    for pattern in _WAF_MARKERS:
        if pattern.search(sample):
            return BlockSignal(BlockKind.WAF, f"waf marker: {pattern.pattern[:40]}")
    for pattern in _LOGIN_MARKERS:
        if pattern.search(sample):
            return BlockSignal(BlockKind.LOGIN_REQUIRED, "login wall")

    if status_code == 200 and len(sample) < min_body_chars and "<html" in sample.lower():
        return BlockSignal(
            BlockKind.EMPTY_SHELL,
            f"suspiciously small html body ({len(sample)} chars)",
            retry_after=15.0,
        )

    return NOT_BLOCKED


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except Exception:
        return None


class BlockedError(RuntimeError):
    """Raised when a job should stop because the site is refusing us."""

    def __init__(self, url: str, signal: BlockSignal) -> None:
        super().__init__(f"blocked at {url}: {signal.kind} ({signal.reason})")
        self.url = url
        self.signal = signal
