"""Rate limiting for login and other abuse-prone routes.

Uses Redis when it is reachable so the limit holds across every API replica,
and falls back to an in-process window otherwise. The fallback is deliberately
still enforced: a single-container deployment must not silently lose
brute-force protection because Redis is absent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - optional dependency at runtime
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    def __init__(self) -> None:
        self._redis = None
        self._redis_unavailable = False
        self._local: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def _client(self):
        if self._redis_unavailable or not settings.REDIS_URL or aioredis is None:
            return None
        if self._redis is None:
            try:
                self._redis = aioredis.from_url(
                    settings.REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await self._redis.ping()
            except Exception as exc:
                logger.warning(
                    "ratelimit_redis_unavailable",
                    error=str(exc),
                    detail="falling back to in-process rate limiting",
                )
                self._redis = None
                self._redis_unavailable = True
                return None
        return self._redis

    async def hit(
        self, key: str, *, limit: int, window_seconds: int
    ) -> RateLimitResult:
        """Register an attempt and report whether it is allowed."""
        client = await self._client()
        if client is not None:
            try:
                return await self._hit_redis(
                    client, key, limit=limit, window_seconds=window_seconds
                )
            except Exception as exc:  # pragma: no cover - Redis went away
                logger.warning("ratelimit_redis_error", error=str(exc))
                self._redis = None
                self._redis_unavailable = True
        return await self._hit_local(key, limit=limit, window_seconds=window_seconds)

    async def _hit_redis(
        self, client, key: str, *, limit: int, window_seconds: int
    ) -> RateLimitResult:
        redis_key = f"infrasight:ratelimit:{key}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key, 1)
            pipe.ttl(redis_key)
            count, ttl = await pipe.execute()
        count = int(count)
        if ttl is None or int(ttl) < 0:
            await client.expire(redis_key, window_seconds)
            ttl = window_seconds
        allowed = count <= limit
        return RateLimitResult(
            allowed=allowed,
            remaining=max(0, limit - count),
            retry_after_seconds=int(ttl) if not allowed else 0,
        )

    async def _hit_local(
        self, key: str, *, limit: int, window_seconds: int
    ) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            bucket = [ts for ts in self._local.get(key, []) if ts > cutoff]
            bucket.append(now)
            self._local[key] = bucket

            # Opportunistic cleanup so an unbounded key space cannot grow
            # forever in a long-running process.
            if len(self._local) > 10_000:
                for stale_key in [
                    k
                    for k, v in self._local.items()
                    if not v or max(v) < cutoff
                ]:
                    self._local.pop(stale_key, None)

        count = len(bucket)
        allowed = count <= limit
        retry_after = 0
        if not allowed:
            retry_after = int(max(0.0, window_seconds - (now - bucket[0]))) + 1
        return RateLimitResult(
            allowed=allowed,
            remaining=max(0, limit - count),
            retry_after_seconds=retry_after,
        )

    async def reset(self, key: str) -> None:
        """Clear a key - called after a successful login."""
        client = await self._client()
        if client is not None:
            try:
                await client.delete(f"infrasight:ratelimit:{key}")
            except Exception:  # pragma: no cover
                pass
        async with self._lock:
            self._local.pop(key, None)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # pragma: no cover
                pass
            self._redis = None


limiter = RateLimiter()


async def check_login_rate_limit(identifier: str) -> RateLimitResult:
    return await limiter.hit(
        f"login:{identifier}",
        limit=settings.LOGIN_RATE_LIMIT_ATTEMPTS,
        window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    )


async def clear_login_rate_limit(identifier: str) -> None:
    await limiter.reset(f"login:{identifier}")
