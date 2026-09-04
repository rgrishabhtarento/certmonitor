"""InfraSight watching itself.

A monitoring tool that runs out of disk stops monitoring, and the failure is
silent: checks simply stop being recorded. So the same questions it asks of
everything else - is it up, is it slow, is it filling - get asked of its own
services.

**What this deliberately does not do is mount the Docker socket.** Handing the
API container `/var/run/docker.sock` would give a `docker stats` view of all
five services in three lines of code, and it would also mean that anyone who
compromised the API had root on the host. That is a bad trade for a resource
graph, so every number here is either self-reported by a process we run, read
from a service over its normal protocol, or measured from a filesystem the
container can already see:

* **API and worker** report their own cgroup CPU and memory. The worker's
  arrive through the heartbeat row it already writes.
* **PostgreSQL** answers over SQL - database size, per-table breakdown,
  connections, cache hit ratio.
* **Redis** answers `INFO`, which includes its own memory and CPU counters.
* **Disk** comes from the container's root filesystem, which on the default
  Compose topology is backed by the same host device as the postgres volume -
  so it is the number that actually matters.

nginx reports nothing, and is listed as such rather than guessed at.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.monitoring import MonitoringResult, WorkerHeartbeat

logger = get_logger(__name__)

CGROUP_V2 = Path("/sys/fs/cgroup")
CGROUP_V1_MEMORY = Path("/sys/fs/cgroup/memory")
CGROUP_V1_CPU = Path("/sys/fs/cgroup/cpuacct")

# CPU percentage is a rate, so it needs two samples. The first call after a
# process starts has nothing to compare against and honestly reports None.
_previous_cpu: dict[str, tuple[float, float]] = {}


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if raw in ("max", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cpu_usage_seconds() -> float | None:
    """Cumulative CPU seconds for this container, from either cgroup layout."""
    stat = CGROUP_V2 / "cpu.stat"
    if stat.exists():
        try:
            for line in stat.read_text().splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1]) / 1_000_000
        except (OSError, ValueError, IndexError):
            return None

    nanoseconds = _read_int(CGROUP_V1_CPU / "cpuacct.usage")
    return nanoseconds / 1_000_000_000 if nanoseconds is not None else None


def _cpu_quota() -> float | None:
    """How many cores this container may use, if a limit is set.

    Without a limit the percentage is relative to every core on the host,
    which is the honest reading - an unlimited container really can use them.
    """
    v2 = CGROUP_V2 / "cpu.max"
    if v2.exists():
        try:
            quota, period = v2.read_text().split()
            if quota == "max":
                return None
            return int(quota) / int(period)
        except (OSError, ValueError):
            return None

    quota = _read_int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    period = _read_int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    if quota and period and quota > 0:
        return quota / period
    return None


def _memory() -> tuple[int | None, int | None]:
    """(used bytes, limit bytes). Either may be None if not exposed."""
    used = _read_int(CGROUP_V2 / "memory.current")
    limit = _read_int(CGROUP_V2 / "memory.max")
    if used is None:
        used = _read_int(CGROUP_V1_MEMORY / "memory.usage_in_bytes")
        limit = _read_int(CGROUP_V1_MEMORY / "memory.limit_in_bytes")
        # cgroup v1 reports "no limit" as a number near 2^63 rather than
        # omitting it; anything that large is not a real limit.
        if limit is not None and limit > 2**60:
            limit = None
    return used, limit


def process_stats(key: str = "api") -> dict[str, Any]:
    """CPU and memory for the container this code is running in.

    ``key`` separates the sampling state, so the API and the worker each
    measure against their own previous reading rather than each other's.
    """
    now = time.monotonic()
    cpu_seconds = _cpu_usage_seconds()
    cores = _cpu_quota() or (os.cpu_count() or 1)

    cpu_percent = None
    if cpu_seconds is not None:
        previous = _previous_cpu.get(key)
        if previous:
            last_seconds, last_at = previous
            elapsed = now - last_at
            # Below a second the sample is mostly quantisation noise.
            if elapsed >= 1.0:
                busy = cpu_seconds - last_seconds
                cpu_percent = round(max(0.0, busy / elapsed / cores * 100), 1)
        _previous_cpu[key] = (cpu_seconds, now)

    used, limit = _memory()
    return {
        "cpu_percent": cpu_percent,
        "cpu_cores": round(cores, 2),
        "memory_mb": round(used / 1024 / 1024, 1) if used else None,
        "memory_limit_mb": round(limit / 1024 / 1024, 1) if limit else None,
        "memory_percent": (
            round(used / limit * 100, 1) if used and limit else None
        ),
    }


# ------------------------------------------------------------------- disk
def disk_usage(path: str = "/") -> dict[str, Any]:
    """Free space on the filesystem backing this container.

    On the bundled Compose topology the container's root and the postgres
    named volume sit on the same host device, so this is the figure that
    decides whether the database can keep writing. If postgres has been moved
    to its own disk or an external service, this reports the API's disk and
    not the database's - which is why the response says which path it read.
    """
    try:
        total, used, free = shutil.disk_usage(path)
    except OSError as exc:
        return {"path": path, "available": False, "error": str(exc)}

    return {
        "path": path,
        "available": True,
        "total_gb": round(total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "free_gb": round(free / 1024**3, 2),
        "used_percent": round(used / total * 100, 1) if total else None,
    }


# --------------------------------------------------------------- database
async def database_stats(session: AsyncSession) -> dict[str, Any]:
    """Size, composition and growth of the database.

    The growth rate and the projection are the useful part. "4.2 GB" says
    nothing on its own; "growing 180 MB a day, 61 days of headroom" is a date
    to act before.
    """
    stats: dict[str, Any] = {"available": True}

    try:
        stats["size_bytes"] = int(
            (
                await session.execute(
                    text("SELECT pg_database_size(current_database())")
                )
            ).scalar()
            or 0
        )
        stats["size_pretty"] = (
            await session.execute(
                text("SELECT pg_size_pretty(pg_database_size(current_database()))")
            )
        ).scalar()

        # Largest tables, including their indexes and TOAST.
        #
        # Every column is qualified. `pg_stat_user_tables` also has a
        # `relname`, so a bare one here is ambiguous and PostgreSQL rejects
        # the whole statement - and the alias is `live_rows` rather than
        # `rows`, which is a reserved word.
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname AS table_name,
                           pg_total_relation_size(c.oid) AS bytes,
                           pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty,
                           COALESCE(s.n_live_tup, 0) AS live_rows
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                    WHERE c.relkind = 'r' AND n.nspname = 'public'
                    ORDER BY pg_total_relation_size(c.oid) DESC
                    LIMIT 10
                    """
                )
            )
        ).all()
        stats["tables"] = [
            {
                "name": name,
                "bytes": int(size),
                "pretty": pretty,
                "rows": int(live_rows),
            }
            for name, size, pretty, live_rows in rows
        ]

        connections = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database()"
                )
            )
        ).scalar()
        # current_setting() rather than SHOW: it is an ordinary SELECT, so it
        # goes through the driver's normal prepared-statement path.
        max_connections = (
            await session.execute(text("SELECT current_setting('max_connections')"))
        ).scalar()
        stats["connections"] = int(connections or 0)
        stats["max_connections"] = int(max_connections or 0)

        # Cache hit ratio. Below ~99% on a workload this size usually means
        # shared_buffers is too small for the working set.
        hit = (
            await session.execute(
                text(
                    """
                    SELECT CASE WHEN blks_hit + blks_read = 0 THEN NULL
                           ELSE round(blks_hit::numeric
                                      / (blks_hit + blks_read) * 100, 2) END
                    FROM pg_stat_database WHERE datname = current_database()
                    """
                )
            )
        ).scalar()
        stats["cache_hit_percent"] = float(hit) if hit is not None else None

    except Exception as exc:
        # The rollback is not optional. PostgreSQL aborts the whole
        # transaction on any statement error, and every later query on this
        # session then fails with "current transaction is aborted" - so
        # swallowing the original error here without resetting would turn one
        # bad query into a 503 for the entire page, attributed to whatever ran
        # next. Clearing the state keeps the failure local to this section.
        logger.warning("database_stats_failed", error=str(exc))
        await session.rollback()
        return {"available": False, "error": str(exc)[:300]}

    # ---- growth, measured from the data rather than assumed
    try:
        oldest = (
            await session.execute(select(func.min(MonitoringResult.checked_at)))
        ).scalar()
        total_results = (
            await session.execute(select(func.count(MonitoringResult.id)))
        ).scalar() or 0

        stats["oldest_result_at"] = oldest
        stats["monitoring_results"] = int(total_results)

        if oldest:
            from datetime import datetime, timezone

            days = max(
                1.0,
                (datetime.now(timezone.utc) - oldest).total_seconds() / 86400,
            )
            per_day = stats["size_bytes"] / days
            stats["growth_bytes_per_day"] = int(per_day)
            stats["retention_days"] = settings.DATA_RETENTION_DAYS
            # Once retention starts pruning, size plateaus rather than growing
            # forever - so a projection only means something before that.
            stats["at_steady_state"] = days >= settings.DATA_RETENTION_DAYS
        else:
            stats["growth_bytes_per_day"] = None
            stats["at_steady_state"] = False
    except Exception as exc:
        logger.warning("database_growth_failed", error=str(exc))
        await session.rollback()
        stats["growth_bytes_per_day"] = None
        stats["at_steady_state"] = False

    return stats


# ------------------------------------------------------------------ redis
async def redis_stats() -> dict[str, Any]:
    """Redis reports its own memory and CPU through INFO.

    Optional infrastructure: the application degrades to in-process rate
    limiting without it, so an unreachable Redis is reported as unavailable
    rather than treated as a fault.
    """
    if not settings.REDIS_URL:
        return {"available": False, "reason": "REDIS_URL is not configured"}

    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        return {"available": False, "reason": "redis client not installed"}

    client = None
    try:
        client = aioredis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
        info = await client.info()
        used = info.get("used_memory")
        limit = info.get("maxmemory") or None
        return {
            "available": True,
            "version": info.get("redis_version"),
            "memory_mb": round(used / 1024 / 1024, 1) if used else None,
            "memory_limit_mb": round(limit / 1024 / 1024, 1) if limit else None,
            "memory_percent": (
                round(used / limit * 100, 1) if used and limit else None
            ),
            "cpu_seconds": round(
                (info.get("used_cpu_sys") or 0) + (info.get("used_cpu_user") or 0), 1
            ),
            "connected_clients": info.get("connected_clients"),
            "uptime_seconds": info.get("uptime_in_seconds"),
            "keys": sum(
                value.get("keys", 0)
                for key, value in info.items()
                if key.startswith("db") and isinstance(value, dict)
            ),
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:160]}
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass


# ----------------------------------------------------------------- worker
async def worker_stats(session: AsyncSession) -> list[dict[str, Any]]:
    """Per-worker resource use, carried on the heartbeat it already writes.

    The worker measures its own cgroup and includes the numbers in the row it
    writes every few seconds, so the API learns them without needing any
    channel to the worker process.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.WORKER_RETIRE_AFTER_SECONDS
    )
    try:
        rows = list(
            (
                await session.execute(
                    select(WorkerHeartbeat)
                    .where(WorkerHeartbeat.last_seen_at >= cutoff)
                    .order_by(WorkerHeartbeat.worker_id)
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:
        # The likely cause is migration 0006 not yet applied, which leaves the
        # model selecting cpu_percent from a table that does not have it.
        # A resource page must not 500 because one of its panels cannot load.
        logger.warning("worker_stats_failed", error=str(exc))
        await session.rollback()
        return []

    now = datetime.now(timezone.utc)
    return [
        {
            "worker_id": row.worker_id,
            "hostname": row.hostname,
            "last_seen_at": row.last_seen_at,
            "seconds_since_heartbeat": int(
                (now - row.last_seen_at).total_seconds()
            ),
            "healthy": (now - row.last_seen_at).total_seconds()
            < settings.WORKER_STALE_AFTER_SECONDS,
            "uptime_seconds": int((now - row.started_at).total_seconds()),
            "checks_completed": row.checks_completed,
            "checks_failed": row.checks_failed,
            "in_flight": row.in_flight,
            "cpu_percent": row.cpu_percent,
            "memory_mb": row.memory_mb,
            "memory_limit_mb": row.memory_limit_mb,
        }
        for row in rows
    ]


# ---------------------------------------------------------------- summary
async def snapshot(session: AsyncSession) -> dict[str, Any]:
    """Everything InfraSight can honestly say about its own resource use."""
    from datetime import datetime, timezone

    disk = disk_usage("/")
    database = await database_stats(session)

    # Headroom, where both numbers are real. Only meaningful before retention
    # starts pruning - after that the database stops growing.
    days_until_full = None
    if (
        disk.get("available")
        and database.get("growth_bytes_per_day")
        and not database.get("at_steady_state")
    ):
        free_bytes = disk["free_gb"] * 1024**3
        days_until_full = int(free_bytes / database["growth_bytes_per_day"])

    return {
        "generated_at": datetime.now(timezone.utc),
        "disk": disk,
        "database": database,
        "redis": await redis_stats(),
        "api": process_stats("api"),
        "workers": await worker_stats(session),
        "days_until_disk_full": days_until_full,
        # Said out loud rather than left as a gap in the UI.
        "not_measured": [
            {
                "service": "frontend (nginx)",
                "reason": (
                    "Static file server with no reporting channel. Measuring "
                    "it would need the Docker socket mounted into the API, "
                    "which would hand host root to anyone who compromised it."
                ),
            },
            {
                "service": "postgres CPU and memory",
                "reason": (
                    "PostgreSQL reports its database size and activity over "
                    "SQL but not its own process resource use. The disk "
                    "figure above covers the failure that actually matters."
                ),
            },
        ],
    }
