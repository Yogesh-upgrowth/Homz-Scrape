"""Retry middleware: exponential backoff + full jitter, with a classifier that
knows which failures are worth retrying.

Retrying a 403 captcha wall just burns quota and annoys the origin, so those
raise immediately. Timeouts, connection resets, 429s and 5xx are retried.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from homz.common.captcha import BlockedError, BlockKind
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
    TimeoutError,
    ConnectionResetError,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: bool = True

    @classmethod
    def from_settings(cls) -> RetryPolicy:
        return cls(max_attempts=max(1, settings.max_retries))

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """`attempt` is 1-based. A server-supplied Retry-After always wins."""
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        raw = self.base_delay * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_delay)
        # Full jitter: uniform(0, capped). Avoids retry storms across workers.
        return random.uniform(0, capped) if self.jitter else capped


class RetryExhausted(RuntimeError):
    def __init__(self, attempts: int, last_error: BaseException | None) -> None:
        super().__init__(f"exhausted {attempts} attempts; last error: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


def should_retry_response(response: httpx.Response) -> bool:
    return response.status_code in RETRYABLE_STATUS


def should_retry_exception(exc: BaseException) -> bool:
    if isinstance(exc, BlockedError):
        return exc.signal.is_retryable
    if isinstance(exc, httpx.HTTPStatusError):
        return should_retry_response(exc.response)
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    context: dict[str, object] | None = None,
    on_retry: Callable[[int, BaseException, float], Awaitable[None]] | None = None,
) -> T:
    """Run `fn`, retrying transient failures with backoff.

    `on_retry(attempt, error, delay)` is the hook the HTTP client uses to
    rotate a proxy or penalize a rate-limit bucket before the next attempt.
    """
    policy = policy or RetryPolicy.from_settings()
    ctx = context or {}
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt | SystemExit):
                raise
            last_error = exc
            if not should_retry_exception(exc) or attempt >= policy.max_attempts:
                raise

            retry_after = None
            if isinstance(exc, BlockedError):
                retry_after = exc.signal.retry_after
            elif isinstance(exc, httpx.HTTPStatusError):
                retry_after = _retry_after_header(exc.response)

            delay = policy.delay_for(attempt, retry_after)
            log.warning(
                "retry.scheduled",
                attempt=attempt,
                max_attempts=policy.max_attempts,
                delay_s=round(delay, 2),
                error=type(exc).__name__,
                detail=str(exc)[:200],
                **ctx,
            )
            if on_retry is not None:
                await on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)

    raise RetryExhausted(policy.max_attempts, last_error)


def _retry_after_header(response: httpx.Response) -> float | None:
    from homz.common.captcha import _parse_retry_after

    return _parse_retry_after(response.headers.get("retry-after"))


def is_hard_block(exc: BaseException) -> bool:
    """A wall we must not hammer: captcha, WAF, or an outright 403."""
    return isinstance(exc, BlockedError) and exc.signal.kind in {
        BlockKind.CAPTCHA,
        BlockKind.WAF,
        BlockKind.FORBIDDEN,
    }
