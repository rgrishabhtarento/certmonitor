"""Availability, latency and dashboard aggregation.

Every number here is derived from rows the worker actually wrote - there is no
synthetic or seeded data anywhere in this module.

Two different notions of "down" are reported on purpose:

* **Uptime %** comes from the ratio of failed to total checks in the window.
  It answers "how often did we observe a problem".
* **Downtime seconds** comes from overlapping incident intervals. It answers
  "for how long was it actually broken", and is what an SLA conversation
  needs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import Float, Integer, case, cast, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import CheckStatus, EndpointStatus, SslStatus
from app.models.endpoint import Endpoint, Environment, Tag, endpoint_tags
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate

WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}

DOWN_STATUSES = (EndpointStatus.DOWN.value,)
SSL_ALERT_STATUSES = (
    SslStatus.EXPIRING_SOON.value,
    SslStatus.CRITICAL.value,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- windows
@dataclass
class AvailabilityStats:
    window: str
    since: datetime
    until: datetime
    total_checks: int = 0
    failed_checks: int = 0
    degraded_checks: int = 0
    uptime_percent: float | None = None
    downtime_seconds: int = 0
    incident_count: int = 0
    avg_response_time_ms: float | None = None
    min_response_time_ms: float | None = None
    max_response_time_ms: float | None = None
    p95_response_time_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "since": self.since,
            "until": self.until,
            "total_checks": self.total_checks,
            "failed_checks": self.failed_checks,
            "degraded_checks": self.degraded_checks,
            "uptime_percent": self.uptime_percent,
            "downtime_seconds": self.downtime_seconds,
            "incident_count": self.incident_count,
            "avg_response_time_ms": self.avg_response_time_ms,
            "min_response_time_ms": self.min_response_time_ms,
            "max_response_time_ms": self.max_response_time_ms,
            "p95_response_time_ms": self.p95_response_time_ms,
        }


def _round(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


async def availability_stats(
    session: AsyncSession,
    endpoint_id: uuid.UUID,
    *,
    window: str = "24h",
    since: datetime | None = None,
    until: datetime | None = None,
) -> AvailabilityStats:
    """Compute availability and latency for one endpoint over one window."""
    until = until or _now()
    if since is None:
        since = until - WINDOWS.get(window, WINDOWS["24h"])

    stats = AvailabilityStats(window=window, since=since, until=until)

    failed_expr = case(
        (MonitoringResult.status == CheckStatus.DOWN.value, 1), else_=0
    )
    degraded_expr = case(
        (MonitoringResult.status == CheckStatus.DEGRADED.value, 1), else_=0
    )

    row = (
        await session.execute(
            select(
                func.count(MonitoringResult.id),
                func.coalesce(func.sum(failed_expr), 0),
                func.coalesce(func.sum(degraded_expr), 0),
                func.avg(MonitoringResult.response_time_ms),
                func.min(MonitoringResult.response_time_ms),
                func.max(MonitoringResult.response_time_ms),
            ).where(
                MonitoringResult.endpoint_id == endpoint_id,
                MonitoringResult.checked_at >= since,
                MonitoringResult.checked_at <= until,
            )
        )
    ).one()

    stats.total_checks = int(row[0] or 0)
    stats.failed_checks = int(row[1] or 0)
    stats.degraded_checks = int(row[2] or 0)
    stats.avg_response_time_ms = _round(row[3])
    stats.min_response_time_ms = _round(row[4])
    stats.max_response_time_ms = _round(row[5])

    if stats.total_checks:
        successful = stats.total_checks - stats.failed_checks
        stats.uptime_percent = round(successful / stats.total_checks * 100.0, 4)

    stats.p95_response_time_ms = await _percentile_response_time(
        session, endpoint_id, since, until, 0.95
    )

    downtime, incident_count = await downtime_in_window(
        session, endpoint_id=endpoint_id, since=since, until=until
    )
    stats.downtime_seconds = downtime
    stats.incident_count = incident_count
    return stats


async def _percentile_response_time(
    session: AsyncSession,
    endpoint_id: uuid.UUID,
    since: datetime,
    until: datetime,
    fraction: float,
) -> float | None:
    """p95 latency, using the native percentile function where available."""
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        value = (
            await session.execute(
                select(
                    func.percentile_cont(fraction).within_group(
                        MonitoringResult.response_time_ms
                    )
                ).where(
                    MonitoringResult.endpoint_id == endpoint_id,
                    MonitoringResult.checked_at >= since,
                    MonitoringResult.checked_at <= until,
                    MonitoringResult.response_time_ms.isnot(None),
                )
            )
        ).scalar()
        return _round(value)

    # Portable fallback (SQLite in tests): fetch the ordered column and index
    # into it. Bounded by the retention window, so this stays small.
    values = (
        await session.execute(
            select(MonitoringResult.response_time_ms)
            .where(
                MonitoringResult.endpoint_id == endpoint_id,
                MonitoringResult.checked_at >= since,
                MonitoringResult.checked_at <= until,
                MonitoringResult.response_time_ms.isnot(None),
            )
            .order_by(MonitoringResult.response_time_ms)
        )
    ).scalars().all()
    if not values:
        return None
    index = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
    return _round(values[index])


async def downtime_in_window(
    session: AsyncSession,
    *,
    endpoint_id: uuid.UUID | None = None,
    since: datetime,
    until: datetime,
) -> tuple[int, int]:
    """Total downtime seconds and incident count overlapping a window.

    Open incidents are clipped at ``until``, and incidents that started before
    the window are clipped at ``since``, so a long outage contributes only the
    portion that falls inside the window.
    """
    stmt = select(Incident.started_at, Incident.resolved_at, Incident.status).where(
        Incident.started_at <= until,
        or_(Incident.resolved_at.is_(None), Incident.resolved_at >= since),
    )
    if endpoint_id is not None:
        stmt = stmt.where(Incident.endpoint_id == endpoint_id)

    rows = (await session.execute(stmt)).all()

    intervals: list[tuple[datetime, datetime]] = []
    for started_at, resolved_at, _status in rows:
        start = max(_aware(started_at) or since, since)
        end = min(_aware(resolved_at) or until, until)
        if end > start:
            intervals.append((start, end))

    # Merge overlaps so concurrent incidents on different endpoints (when
    # endpoint_id is None) are not double-counted.
    intervals.sort()
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in intervals:
        if current_start is None:
            current_start, current_end = start, end
            continue
        if start <= current_end:  # type: ignore[operator]
            current_end = max(current_end, end)  # type: ignore[type-var]
        else:
            total += (current_end - current_start).total_seconds()  # type: ignore[operator]
            current_start, current_end = start, end
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()

    return int(total), len(rows)


async def availability_summary(
    session: AsyncSession, endpoint_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    """The 24h / 7d / 30d / 90d block shown on the endpoint detail page."""
    summary: dict[str, dict[str, Any]] = {}
    for name in WINDOWS:
        stats = await availability_stats(session, endpoint_id, window=name)
        summary[name] = stats.as_dict()
    return summary


# --------------------------------------------------------- time series
def _bucket_expression(session: AsyncSession, bucket_seconds: int):
    """Dialect-aware epoch bucket index for grouping a time series."""
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        return cast(
            func.floor(
                func.extract("epoch", MonitoringResult.checked_at) / bucket_seconds
            ),
            Integer,
        )
    if dialect == "sqlite":
        return cast(
            func.strftime("%s", MonitoringResult.checked_at) / bucket_seconds, Integer
        )
    return cast(
        func.floor(
            func.extract("epoch", MonitoringResult.checked_at) / bucket_seconds
        ),
        Integer,
    )


def choose_bucket_seconds(span: timedelta, *, target_points: int = 120) -> int:
    """Pick a bucket size that yields a readable number of points."""
    raw = max(60, int(span.total_seconds() // max(1, target_points)))
    for candidate in (60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400):
        if raw <= candidate:
            return candidate
    return 86400


async def response_time_series(
    session: AsyncSession,
    endpoint_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime | None = None,
    bucket_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Bucketed latency + failure counts for the response-time graph."""
    until = until or _now()
    bucket_seconds = bucket_seconds or choose_bucket_seconds(until - since)
    bucket = _bucket_expression(session, bucket_seconds).label("bucket")

    failed_expr = case((MonitoringResult.status == CheckStatus.DOWN.value, 1), else_=0)
    degraded_expr = case(
        (MonitoringResult.status == CheckStatus.DEGRADED.value, 1), else_=0
    )

    rows = (
        await session.execute(
            select(
                bucket,
                func.count(MonitoringResult.id),
                func.avg(MonitoringResult.response_time_ms),
                func.min(MonitoringResult.response_time_ms),
                func.max(MonitoringResult.response_time_ms),
                func.coalesce(func.sum(failed_expr), 0),
                func.coalesce(func.sum(degraded_expr), 0),
                func.avg(MonitoringResult.dns_time_ms),
                func.avg(MonitoringResult.connect_time_ms),
                func.avg(MonitoringResult.tls_time_ms),
            )
            .where(
                MonitoringResult.endpoint_id == endpoint_id,
                MonitoringResult.checked_at >= since,
                MonitoringResult.checked_at <= until,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    series: list[dict[str, Any]] = []
    for row in rows:
        bucket_index = int(row[0])
        timestamp = datetime.fromtimestamp(
            bucket_index * bucket_seconds, tz=timezone.utc
        )
        total = int(row[1] or 0)
        failed = int(row[5] or 0)
        series.append(
            {
                "timestamp": timestamp,
                "checks": total,
                "avg_response_time_ms": _round(row[2]),
                "min_response_time_ms": _round(row[3]),
                "max_response_time_ms": _round(row[4]),
                "failed_checks": failed,
                "degraded_checks": int(row[6] or 0),
                "uptime_percent": round((total - failed) / total * 100.0, 2)
                if total
                else None,
                "avg_dns_time_ms": _round(row[7]),
                "avg_connect_time_ms": _round(row[8]),
                "avg_tls_time_ms": _round(row[9]),
            }
        )
    return series


async def global_response_time_series(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    endpoint_ids: Sequence[uuid.UUID] | None = None,
    bucket_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Fleet-wide latency and failure trend for the dashboard charts."""
    until = until or _now()
    bucket_seconds = bucket_seconds or choose_bucket_seconds(until - since)
    bucket = _bucket_expression(session, bucket_seconds).label("bucket")

    failed_expr = case((MonitoringResult.status == CheckStatus.DOWN.value, 1), else_=0)

    stmt = (
        select(
            bucket,
            func.count(MonitoringResult.id),
            func.avg(MonitoringResult.response_time_ms),
            func.coalesce(func.sum(failed_expr), 0),
        )
        .where(
            MonitoringResult.checked_at >= since,
            MonitoringResult.checked_at <= until,
        )
        .group_by(bucket)
        .order_by(bucket)
    )
    if endpoint_ids is not None:
        if not endpoint_ids:
            return []
        stmt = stmt.where(MonitoringResult.endpoint_id.in_(list(endpoint_ids)))

    rows = (await session.execute(stmt)).all()
    series: list[dict[str, Any]] = []
    for row in rows:
        total = int(row[1] or 0)
        failed = int(row[3] or 0)
        series.append(
            {
                "timestamp": datetime.fromtimestamp(
                    int(row[0]) * bucket_seconds, tz=timezone.utc
                ),
                "checks": total,
                "avg_response_time_ms": _round(row[2]),
                "failed_checks": failed,
                "uptime_percent": round((total - failed) / total * 100.0, 2)
                if total
                else None,
            }
        )
    return series


# ----------------------------------------------------------- dashboard
@dataclass
class DashboardFilters:
    environment_ids: list[uuid.UUID] = field(default_factory=list)
    tag_ids: list[uuid.UUID] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.environment_ids,
                self.tag_ids,
                self.owners,
                self.teams,
                self.applications,
                self.statuses,
            )
        )


def apply_endpoint_filters(stmt, filters: DashboardFilters):
    """Attach dashboard filters to any statement selecting from endpoints."""
    if filters.environment_ids:
        stmt = stmt.where(Endpoint.environment_id.in_(filters.environment_ids))
    if filters.owners:
        stmt = stmt.where(Endpoint.owner.in_(filters.owners))
    if filters.teams:
        stmt = stmt.where(Endpoint.team.in_(filters.teams))
    if filters.applications:
        stmt = stmt.where(Endpoint.application.in_(filters.applications))
    if filters.statuses:
        stmt = stmt.where(Endpoint.current_status.in_(filters.statuses))
    if filters.tag_ids:
        # EXISTS rather than a join: a join would multiply rows for endpoints
        # carrying several matching tags and corrupt the counts.
        stmt = stmt.where(
            select(endpoint_tags.c.endpoint_id)
            .where(
                endpoint_tags.c.endpoint_id == Endpoint.id,
                endpoint_tags.c.tag_id.in_(filters.tag_ids),
            )
            .exists()
        )
    return stmt


async def filtered_endpoint_ids(
    session: AsyncSession, filters: DashboardFilters
) -> list[uuid.UUID] | None:
    """Endpoint ids matching the filters, or ``None`` when unfiltered."""
    if filters.is_empty:
        return None
    stmt = apply_endpoint_filters(select(Endpoint.id), filters)
    return list((await session.execute(stmt)).scalars().all())


async def dashboard_summary(
    session: AsyncSession,
    *,
    filters: DashboardFilters | None = None,
    window: str = "24h",
) -> dict[str, Any]:
    """The summary cards at the top of the dashboard."""
    filters = filters or DashboardFilters()
    until = _now()
    since = until - WINDOWS.get(window, WINDOWS["24h"])

    status_rows = (
        await session.execute(
            apply_endpoint_filters(
                select(Endpoint.current_status, func.count(Endpoint.id)), filters
            ).group_by(Endpoint.current_status)
        )
    ).all()
    status_counts = {str(row[0]): int(row[1]) for row in status_rows}

    paused_count = int(
        (
            await session.execute(
                apply_endpoint_filters(
                    select(func.count(Endpoint.id)), filters
                ).where(
                    or_(
                        Endpoint.is_paused.is_(True),
                        Endpoint.monitoring_enabled.is_(False),
                    )
                )
            )
        ).scalar()
        or 0
    )

    total_endpoints = sum(status_counts.values())

    ssl_rows = (
        await session.execute(
            apply_endpoint_filters(
                select(Endpoint.ssl_status, func.count(Endpoint.id)), filters
            )
            .where(Endpoint.ssl_monitoring_enabled.is_(True))
            .group_by(Endpoint.ssl_status)
        )
    ).all()
    ssl_counts = {str(row[0]): int(row[1]) for row in ssl_rows}
    ssl_tracked = sum(
        count
        for status, count in ssl_counts.items()
        if status != SslStatus.NOT_APPLICABLE.value
    )

    endpoint_ids = await filtered_endpoint_ids(session, filters)

    latency_stmt = select(
        func.avg(MonitoringResult.response_time_ms),
        func.count(MonitoringResult.id),
        func.coalesce(
            func.sum(
                case((MonitoringResult.status == CheckStatus.DOWN.value, 1), else_=0)
            ),
            0,
        ),
    ).where(
        MonitoringResult.checked_at >= since,
        MonitoringResult.checked_at <= until,
    )
    if endpoint_ids is not None:
        if endpoint_ids:
            latency_stmt = latency_stmt.where(
                MonitoringResult.endpoint_id.in_(endpoint_ids)
            )
        else:
            latency_stmt = latency_stmt.where(false())

    latency_row = (await session.execute(latency_stmt)).one()
    avg_response = _round(latency_row[0])
    total_checks = int(latency_row[1] or 0)
    failed_checks = int(latency_row[2] or 0)
    overall_uptime = (
        round((total_checks - failed_checks) / total_checks * 100.0, 4)
        if total_checks
        else None
    )

    open_incident_stmt = select(func.count(Incident.id)).where(
        Incident.status == "open"
    )
    if endpoint_ids is not None:
        if endpoint_ids:
            open_incident_stmt = open_incident_stmt.where(
                Incident.endpoint_id.in_(endpoint_ids)
            )
        else:
            open_incident_stmt = open_incident_stmt.where(false())
    open_incidents = int((await session.execute(open_incident_stmt)).scalar() or 0)

    return {
        "window": window,
        "since": since,
        "until": until,
        "total_endpoints": total_endpoints,
        "healthy": status_counts.get(EndpointStatus.UP.value, 0),
        "down": status_counts.get(EndpointStatus.DOWN.value, 0),
        "degraded": status_counts.get(EndpointStatus.DEGRADED.value, 0),
        "unknown": status_counts.get(EndpointStatus.UNKNOWN.value, 0),
        "paused": paused_count,
        "ssl_certificates": ssl_tracked,
        "ssl_valid": ssl_counts.get(SslStatus.VALID.value, 0),
        "ssl_expiring_soon": ssl_counts.get(SslStatus.EXPIRING_SOON.value, 0),
        "ssl_critical": ssl_counts.get(SslStatus.CRITICAL.value, 0),
        "ssl_expired": ssl_counts.get(SslStatus.EXPIRED.value, 0),
        "ssl_invalid": ssl_counts.get(SslStatus.INVALID.value, 0),
        "ssl_unable_to_check": ssl_counts.get(SslStatus.UNABLE_TO_CHECK.value, 0),
        "ssl_alerts": (
            ssl_counts.get(SslStatus.EXPIRING_SOON.value, 0)
            + ssl_counts.get(SslStatus.CRITICAL.value, 0)
            + ssl_counts.get(SslStatus.EXPIRED.value, 0)
            + ssl_counts.get(SslStatus.INVALID.value, 0)
        ),
        "average_response_time_ms": avg_response,
        "overall_uptime_percent": overall_uptime,
        "total_checks": total_checks,
        "failed_checks": failed_checks,
        "open_incidents": open_incidents,
        "status_distribution": [
            {"status": status, "count": count}
            for status, count in sorted(status_counts.items())
        ],
    }


async def availability_by_group(
    session: AsyncSession,
    *,
    group: str,
    filters: DashboardFilters | None = None,
    window: str = "24h",
) -> list[dict[str, Any]]:
    """Availability broken down by environment, tag, team or owner."""
    filters = filters or DashboardFilters()
    until = _now()
    since = until - WINDOWS.get(window, WINDOWS["24h"])

    down_expr = case((Endpoint.current_status == EndpointStatus.DOWN.value, 1), else_=0)
    degraded_expr = case(
        (Endpoint.current_status == EndpointStatus.DEGRADED.value, 1), else_=0
    )
    up_expr = case((Endpoint.current_status == EndpointStatus.UP.value, 1), else_=0)

    if group == "tag":
        stmt = (
            select(
                Tag.id,
                Tag.name,
                func.count(Endpoint.id),
                func.coalesce(func.sum(up_expr), 0),
                func.coalesce(func.sum(down_expr), 0),
                func.coalesce(func.sum(degraded_expr), 0),
                func.avg(Endpoint.last_response_time_ms),
            )
            .select_from(Tag)
            .join(endpoint_tags, endpoint_tags.c.tag_id == Tag.id)
            .join(Endpoint, Endpoint.id == endpoint_tags.c.endpoint_id)
            .group_by(Tag.id, Tag.name)
            .order_by(Tag.name)
        )
    elif group == "environment":
        stmt = (
            select(
                Environment.id,
                Environment.name,
                func.count(Endpoint.id),
                func.coalesce(func.sum(up_expr), 0),
                func.coalesce(func.sum(down_expr), 0),
                func.coalesce(func.sum(degraded_expr), 0),
                func.avg(Endpoint.last_response_time_ms),
            )
            .select_from(Environment)
            .join(Endpoint, Endpoint.environment_id == Environment.id)
            .group_by(Environment.id, Environment.name)
            .order_by(Environment.sort_order, Environment.name)
        )
    elif group in ("team", "owner", "application"):
        column = getattr(Endpoint, group)
        stmt = (
            select(
                func.coalesce(column, "Unassigned"),
                func.coalesce(column, "Unassigned"),
                func.count(Endpoint.id),
                func.coalesce(func.sum(up_expr), 0),
                func.coalesce(func.sum(down_expr), 0),
                func.coalesce(func.sum(degraded_expr), 0),
                func.avg(Endpoint.last_response_time_ms),
            )
            .group_by(func.coalesce(column, "Unassigned"))
            .order_by(func.coalesce(column, "Unassigned"))
        )
    else:
        raise ValueError(f"unsupported grouping '{group}'")

    stmt = apply_endpoint_filters(stmt, filters)
    rows = (await session.execute(stmt)).all()

    groups: list[dict[str, Any]] = []
    for row in rows:
        total = int(row[2] or 0)
        healthy = int(row[3] or 0)
        groups.append(
            {
                "id": str(row[0]) if row[0] is not None else None,
                "name": row[1],
                "total": total,
                "healthy": healthy,
                "down": int(row[4] or 0),
                "degraded": int(row[5] or 0),
                "avg_response_time_ms": _round(row[6]),
                "health_percent": round(healthy / total * 100.0, 2) if total else None,
            }
        )

    # Observed uptime per group over the window, from the results table.
    for entry in groups:
        entry["uptime_percent"] = await _group_uptime(
            session, group=group, group_key=entry, since=since, until=until,
            filters=filters,
        )
    return groups


async def _group_uptime(
    session: AsyncSession,
    *,
    group: str,
    group_key: dict[str, Any],
    since: datetime,
    until: datetime,
    filters: DashboardFilters,
) -> float | None:
    """Observed uptime for one dashboard group over the window."""
    endpoint_stmt = select(Endpoint.id)
    if group == "environment":
        if group_key["id"] is None:
            return None
        endpoint_stmt = endpoint_stmt.where(
            Endpoint.environment_id == uuid.UUID(group_key["id"])
        )
    elif group == "tag":
        endpoint_stmt = endpoint_stmt.where(
            select(endpoint_tags.c.endpoint_id)
            .where(
                endpoint_tags.c.endpoint_id == Endpoint.id,
                endpoint_tags.c.tag_id == uuid.UUID(group_key["id"]),
            )
            .exists()
        )
    else:
        column = getattr(Endpoint, group)
        if group_key["name"] == "Unassigned":
            endpoint_stmt = endpoint_stmt.where(column.is_(None))
        else:
            endpoint_stmt = endpoint_stmt.where(column == group_key["name"])

    endpoint_stmt = apply_endpoint_filters(endpoint_stmt, filters)
    ids = list((await session.execute(endpoint_stmt)).scalars().all())
    if not ids:
        return None

    row = (
        await session.execute(
            select(
                func.count(MonitoringResult.id),
                func.coalesce(
                    func.sum(
                        case(
                            (MonitoringResult.status == CheckStatus.DOWN.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                MonitoringResult.endpoint_id.in_(ids),
                MonitoringResult.checked_at >= since,
                MonitoringResult.checked_at <= until,
            )
        )
    ).one()
    total = int(row[0] or 0)
    if not total:
        return None
    return round((total - int(row[1] or 0)) / total * 100.0, 3)


async def ssl_expiry_timeline(
    session: AsyncSession,
    *,
    filters: DashboardFilters | None = None,
    horizon_days: int = 120,
) -> list[dict[str, Any]]:
    """Certificates bucketed by how soon they expire."""
    filters = filters or DashboardFilters()
    buckets = [
        ("expired", None, -1),
        ("0-7 days", 0, 7),
        ("8-14 days", 8, 14),
        ("15-30 days", 15, 30),
        ("31-60 days", 31, 60),
        ("61-90 days", 61, 90),
        (f"91-{horizon_days} days", 91, horizon_days),
        (f"{horizon_days}+ days", horizon_days + 1, None),
    ]

    rows = (
        await session.execute(
            apply_endpoint_filters(
                select(Endpoint.ssl_days_remaining, func.count(Endpoint.id)), filters
            )
            .where(
                Endpoint.ssl_monitoring_enabled.is_(True),
                Endpoint.ssl_days_remaining.isnot(None),
            )
            .group_by(Endpoint.ssl_days_remaining)
        )
    ).all()

    counts = {int(row[0]): int(row[1]) for row in rows}
    timeline: list[dict[str, Any]] = []
    for label, low, high in buckets:
        if label == "expired":
            count = sum(c for days, c in counts.items() if days < 0)
        elif high is None:
            count = sum(c for days, c in counts.items() if days >= low)
        else:
            count = sum(c for days, c in counts.items() if low <= days <= high)
        timeline.append({"bucket": label, "count": count})
    return timeline


async def failure_counts(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    filters: DashboardFilters | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Endpoints with the most failed checks in the window."""
    filters = filters or DashboardFilters()
    until = until or _now()

    stmt = (
        select(
            Endpoint.id,
            Endpoint.name,
            Endpoint.url,
            func.count(MonitoringResult.id).label("failures"),
        )
        .select_from(Endpoint)
        .join(MonitoringResult, MonitoringResult.endpoint_id == Endpoint.id)
        .where(
            MonitoringResult.status == CheckStatus.DOWN.value,
            MonitoringResult.checked_at >= since,
            MonitoringResult.checked_at <= until,
        )
        .group_by(Endpoint.id, Endpoint.name, Endpoint.url)
        .order_by(func.count(MonitoringResult.id).desc())
        .limit(limit)
    )
    stmt = apply_endpoint_filters(stmt, filters)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "endpoint_id": str(row[0]),
            "name": row[1],
            "url": row[2],
            "failed_checks": int(row[3]),
        }
        for row in rows
    ]


async def slowest_endpoints(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    filters: DashboardFilters | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filters = filters or DashboardFilters()
    until = until or _now()
    stmt = (
        select(
            Endpoint.id,
            Endpoint.name,
            Endpoint.url,
            func.avg(MonitoringResult.response_time_ms),
            func.count(MonitoringResult.id),
        )
        .select_from(Endpoint)
        .join(MonitoringResult, MonitoringResult.endpoint_id == Endpoint.id)
        .where(
            MonitoringResult.checked_at >= since,
            MonitoringResult.checked_at <= until,
            MonitoringResult.response_time_ms.isnot(None),
        )
        .group_by(Endpoint.id, Endpoint.name, Endpoint.url)
        .having(func.count(MonitoringResult.id) > 0)
        .order_by(func.avg(MonitoringResult.response_time_ms).desc())
        .limit(limit)
    )
    stmt = apply_endpoint_filters(stmt, filters)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "endpoint_id": str(row[0]),
            "name": row[1],
            "url": row[2],
            "avg_response_time_ms": _round(row[3]),
            "checks": int(row[4]),
        }
        for row in rows
    ]


async def uptime_for_endpoints(
    session: AsyncSession,
    endpoint_ids: Iterable[uuid.UUID],
    *,
    since: datetime,
    until: datetime | None = None,
) -> dict[uuid.UUID, float | None]:
    """Uptime percentage per endpoint, in one query.

    Used by the endpoint list so rendering a page of 50 endpoints costs one
    query instead of fifty.
    """
    ids = list(endpoint_ids)
    if not ids:
        return {}
    until = until or _now()

    rows = (
        await session.execute(
            select(
                MonitoringResult.endpoint_id,
                func.count(MonitoringResult.id),
                func.coalesce(
                    func.sum(
                        case(
                            (MonitoringResult.status == CheckStatus.DOWN.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.avg(cast(MonitoringResult.response_time_ms, Float)),
            )
            .where(
                MonitoringResult.endpoint_id.in_(ids),
                MonitoringResult.checked_at >= since,
                MonitoringResult.checked_at <= until,
            )
            .group_by(MonitoringResult.endpoint_id)
        )
    ).all()

    result: dict[uuid.UUID, float | None] = {}
    for endpoint_id, total, failed, _avg in rows:
        total = int(total or 0)
        result[endpoint_id] = (
            round((total - int(failed or 0)) / total * 100.0, 3) if total else None
        )
    return result


async def certificate_summary(session: AsyncSession) -> dict[str, int]:
    """Counts per certificate state, for the SSL dashboard header."""
    rows = (
        await session.execute(
            select(SslCertificate.status, func.count(SslCertificate.id))
            .where(SslCertificate.is_current.is_(True))
            .group_by(SslCertificate.status)
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def recent_incidents(
    session: AsyncSession,
    *,
    limit: int = 10,
    filters: DashboardFilters | None = None,
    open_only: bool = False,
) -> list[Incident]:
    filters = filters or DashboardFilters()
    stmt = (
        select(Incident)
        .join(Endpoint, Endpoint.id == Incident.endpoint_id)
        .order_by(Incident.started_at.desc())
        .limit(limit)
    )
    if open_only:
        stmt = stmt.where(Incident.status == "open")
    stmt = apply_endpoint_filters(stmt, filters)
    return list((await session.execute(stmt)).scalars().all())


async def endpoint_incident_history(
    session: AsyncSession,
    endpoint_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Incident], int]:
    total = int(
        (
            await session.execute(
                select(func.count(Incident.id)).where(
                    Incident.endpoint_id == endpoint_id
                )
            )
        ).scalar()
        or 0
    )
    rows = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.endpoint_id == endpoint_id)
                .order_by(Incident.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return rows, total


def sla_breaches(
    groups: list[dict[str, Any]], *, target: float
) -> list[dict[str, Any]]:
    """Groups whose observed uptime is below the configured SLA target."""
    return [
        g
        for g in groups
        if g.get("uptime_percent") is not None and g["uptime_percent"] < target
    ]


__all__ = [
    "AvailabilityStats",
    "DashboardFilters",
    "WINDOWS",
    "apply_endpoint_filters",
    "availability_by_group",
    "availability_stats",
    "availability_summary",
    "certificate_summary",
    "choose_bucket_seconds",
    "dashboard_summary",
    "downtime_in_window",
    "endpoint_incident_history",
    "failure_counts",
    "filtered_endpoint_ids",
    "global_response_time_series",
    "recent_incidents",
    "response_time_series",
    "sla_breaches",
    "slowest_endpoints",
    "ssl_expiry_timeline",
    "uptime_for_endpoints",
]
