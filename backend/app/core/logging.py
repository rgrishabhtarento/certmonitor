"""Structured logging setup.

Emits JSON in production (easy to ship to Loki/ELK) and human-readable lines in
development. A processor scrubs anything that looks like a secret so tokens
never reach the log stream.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import settings

_SENSITIVE_KEYS = {
    "password",
    "new_password",
    "current_password",
    "old_password",
    "admin_password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "auth_secret",
    "auth_credentials",
    "secret",
    "jwt_secret",
    "encryption_key",
    "database_url",
    "api_key",
    "bearer_token",
    "basic_password",
}

_REDACTED = "***redacted***"


def _redact(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
        elif isinstance(event_dict[key], dict):
            event_dict[key] = {
                k: (_REDACTED if k.lower() in _SENSITIVE_KEYS else v)
                for k, v in event_dict[key].items()
            }
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    # Quiet the chatty libraries; request lines are emitted by our own
    # middleware with the fields we actually care about.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.LOG_FORMAT == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
