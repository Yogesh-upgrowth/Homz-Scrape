"""structlog configuration.

Console renderer in dev, JSON in prod. Every log line carries the bound context
(source, job, url) so a failed page can be traced end-to-end.
"""

from __future__ import annotations

import logging
import sys

import structlog

from homz.settings import settings

_CONFIGURED = False


def configure_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = (level or settings.log_level).upper()
    json_logs = settings.log_json if json_logs is None else json_logs

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # Third-party loggers are noisy at DEBUG; keep them at WARNING.
    for noisy in ("httpx", "httpcore", "asyncio", "anthropic", "pymongo", "motor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)
