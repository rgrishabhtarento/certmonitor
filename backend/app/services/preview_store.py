"""Short-lived store for import previews.

The import flow is validate-then-confirm, so the validated rows have to
survive between two requests without being written to the endpoints table.
Redis holds them when available, which keeps the flow correct across API
replicas; otherwise an in-process TTL cache is used and the confirm request
must reach the same replica (Docker Compose runs a single API container, and
the Kubernetes notes in the README cover the multi-replica case).
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TTL_SECONDS = 900  # 15 minutes: long enough to review a large preview
_KEY_PREFIX = "infrasight:import-preview:"
_MAX_LOCAL_ENTRIES = 64

try:  # pragma: no cover - optional at runtime
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

_local: dict[str, tuple[float, dict[str, Any]]] = {}
_redis = None
_redis_unavailable = False


async def _client():
    global _redis, _redis_unavailable
    if _redis_unavailable or not settings.REDIS_URL or aioredis is None:
        return None
    if _redis is None:
        try:
            _redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await _redis.ping()
        except Exception as exc:
            logger.warning(
                "preview_store_redis_unavailable",
                error=str(exc),
                detail="import previews will be held in process memory",
            )
            _redis = None
            _redis_unavailable = True
            return None
    return _redis


def _prune_local() -> None:
    now = time.monotonic()
    for key in [k for k, (expires, _) in _local.items() if expires <= now]:
        _local.pop(key, None)
    # Hard cap so a burst of abandoned previews cannot grow without bound.
    while len(_local) > _MAX_LOCAL_ENTRIES:
        oldest = min(_local, key=lambda k: _local[k][0])
        _local.pop(oldest, None)


async def save(payload: dict[str, Any]) -> tuple[str, datetime]:
    """Store a preview and return ``(token, expires_at)``."""
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)

    client = await _client()
    if client is not None:
        try:
            await client.set(
                _KEY_PREFIX + token,
                json.dumps(payload, default=str),
                ex=TTL_SECONDS,
            )
            return token, expires_at
        except Exception as exc:  # pragma: no cover
            logger.warning("preview_store_write_failed", error=str(exc))

    _prune_local()
    _local[token] = (time.monotonic() + TTL_SECONDS, payload)
    return token, expires_at


async def load(token: str) -> dict[str, Any] | None:
    client = await _client()
    if client is not None:
        try:
            raw = await client.get(_KEY_PREFIX + token)
            if raw:
                return json.loads(raw)
        except Exception as exc:  # pragma: no cover
            logger.warning("preview_store_read_failed", error=str(exc))

    _prune_local()
    entry = _local.get(token)
    return entry[1] if entry else None


async def discard(token: str) -> None:
    """Remove a preview once it has been confirmed."""
    client = await _client()
    if client is not None:
        try:
            await client.delete(_KEY_PREFIX + token)
        except Exception:  # pragma: no cover
            pass
    _local.pop(token, None)
