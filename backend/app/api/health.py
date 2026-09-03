"""Health, readiness and worker status.

``/health`` and ``/ready`` are unauthenticated on purpose: Docker and
Kubernetes probes cannot present a token. They deliberately expose no
configuration, no counts and no hostnames beyond component states.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from time import perf_counter

from fastapi import APIRouter, Response, status
from sqlalchemy import select, text

from app.api.deps import DbSession, ReadSettings
from app.core.config import settings
from app.core.logging import get_logger
from app.models.monitoring import WorkerHeartbeat
from app.schemas.dashboard import ComponentHealth, HealthResponse, WorkerStatus

logger = get_logger(__name__)

router = APIRouter(tags=["Health"])

APP_VERSION = "1.0.0"
_STARTED_AT = time.monotonic()

_HEALTHY = "healthy"
_DEGRADED = "degraded"
_UNHEALTHY = "unhealthy"


async def _check_database(session) -> ComponentHealth:
    started = perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        return ComponentHealth(
            status=_HEALTHY,
            latency_ms=round((perf_counter() - started) * 1000.0, 2),
        )
    except Exception as exc:
        logger.error("health_database_unreachable", error=str(exc))
        return ComponentHealth(
            status=_UNHEALTHY,
            detail="database is unreachable",
            latency_ms=round((perf_counter() - started) * 1000.0, 2),
        )


async def _check_worker(session) -> ComponentHealth:
    """Report worker liveness from its heartbeat rows.

    The API has no direct channel to the worker - which is the point of
    separating them - so liveness is inferred from the heartbeat each worker
    writes on every cycle.
    """
    try:
        rows = (await session.execute(select(WorkerHeartbeat))).scalars().all()
    except Exception:
        return ComponentHealth(
            status=_UNHEALTHY, detail="cannot read worker heartbeats"
        )

    if not rows:
        return ComponentHealth(
            status=_UNHEALTHY,
            detail="no monitoring worker has registered yet",
        )

    now = datetime.now(timezone.utc)
    fresh: list[str] = []
    stale: list[str] = []
    # A worker gets a fresh identity whenever its container is recreated, so
    # every rebuild leaves its predecessor's row behind. Those rows are history,
    # not a fault: past the retire window they are ignored here (and deleted by
    # the worker's own sweep). Without this, one rebuild would pin the status at
    # "degraded" permanently.
    retire_after = max(
        settings.WORKER_STALE_AFTER_SECONDS * 10,
        settings.WORKER_RETIRE_AFTER_SECONDS,
    )
    retired = 0
    for row in rows:
        last_seen = row.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age = (now - last_seen).total_seconds()
        if age <= settings.WORKER_STALE_AFTER_SECONDS:
            fresh.append(row.worker_id)
        elif age <= retire_after:
            stale.append(row.worker_id)
        else:
            retired += 1

    if not fresh and not stale:
        return ComponentHealth(
            status=_UNHEALTHY,
            detail=(
                f"no worker heartbeat within {settings.WORKER_STALE_AFTER_SECONDS}s"
                + (f" ({retired} retired worker record(s))" if retired else "")
            ),
        )

    if fresh and not stale:
        return ComponentHealth(
            status=_HEALTHY, detail=f"{len(fresh)} worker(s) reporting"
        )
    if fresh:
        return ComponentHealth(
            status=_DEGRADED,
            detail=f"{len(fresh)} worker(s) healthy, {len(stale)} stale",
        )
    return ComponentHealth(
        status=_UNHEALTHY,
        detail=(
            f"no worker heartbeat within {settings.WORKER_STALE_AFTER_SECONDS}s"
        ),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and dependency health",
)
async def health(response: Response, session: DbSession) -> HealthResponse:
    """Overall application health.

    Returns 200 when the application can serve traffic and 503 when the
    database is unreachable. A stale worker degrades the response but does not
    fail it - the API is still able to serve the dashboard and accept
    configuration changes.
    """
    database = await _check_database(session)
    worker = await _check_worker(session)

    if database.status == _UNHEALTHY:
        overall = _UNHEALTHY
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif worker.status != _HEALTHY:
        overall = _DEGRADED
    else:
        overall = _HEALTHY

    return HealthResponse(
        status=overall,
        version=APP_VERSION,
        environment=settings.APP_ENV,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 2),
        database=database.status,
        monitoring_worker=worker.status,
        components={"database": database, "monitoring_worker": worker},
        checked_at=datetime.now(timezone.utc),
    )


@router.get(
    "/ready",
    summary="Readiness for traffic",
    responses={
        200: {"description": "Ready"},
        503: {"description": "Not ready"},
    },
)
async def ready(response: Response, session: DbSession) -> dict:
    """Readiness probe.

    Requires the schema to be present and the seed data to exist, so a
    container that started before migrations completed is kept out of the load
    balancer rather than serving 500s.
    """
    checks: dict[str, str] = {}
    ok = True

    database = await _check_database(session)
    checks["database"] = database.status
    ok = ok and database.status == _HEALTHY

    if ok:
        try:
            # A seeded role proves migrations ran and bootstrap completed.
            from app.models.user import Role

            role = (
                await session.execute(select(Role.id).limit(1))
            ).first()
            checks["schema"] = _HEALTHY if role is not None else _UNHEALTHY
            if role is None:
                checks["detail"] = "database schema present but not seeded yet"
                ok = False
        except Exception as exc:
            checks["schema"] = _UNHEALTHY
            checks["detail"] = "database schema is missing - run migrations"
            logger.error("readiness_schema_check_failed", error=str(exc))
            ok = False

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ok else "not_ready",
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get(
    "/live",
    summary="Process liveness (no dependency checks)",
)
async def live() -> dict:
    """Cheapest possible probe: is this process still answering?

    Kept separate from ``/health`` so a database outage does not cause
    Kubernetes to restart otherwise-healthy API pods.
    """
    return {"status": "alive", "uptime_seconds": round(time.monotonic() - _STARTED_AT, 2)}


@router.get(
    "/api/workers",
    response_model=list[WorkerStatus],
    summary="Monitoring worker fleet status",
)
async def workers(session: DbSession, _user: ReadSettings) -> list[WorkerStatus]:
    """Per-worker heartbeat detail, for the settings/diagnostics screen."""
    rows = (
        await session.execute(
            select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id)
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    result: list[WorkerStatus] = []
    for row in rows:
        last_seen = row.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age = (now - last_seen).total_seconds()
        result.append(
            WorkerStatus(
                worker_id=row.worker_id,
                hostname=row.hostname,
                version=row.version,
                started_at=row.started_at,
                last_seen_at=last_seen,
                seconds_since_heartbeat=round(age, 1),
                is_healthy=age <= settings.WORKER_STALE_AFTER_SECONDS,
                checks_completed=row.checks_completed,
                checks_failed=row.checks_failed,
                in_flight=row.in_flight,
            )
        )
    return result
