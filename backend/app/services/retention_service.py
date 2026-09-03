"""Scheduled cleanup of historical monitoring data.

High-frequency check results are the only table that grows without bound - a
thousand endpoints on a 60-second interval write about 1.4 million rows a day -
so they are pruned aggressively. Incidents and audit logs are kept far longer
because they are the record of what happened, and they are small.

Deletes run in bounded batches so the sweep never holds a long transaction or a
table-wide lock while the worker is trying to write results.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.alert import Alert
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate
from app.models.system import AuditLog

logger = get_logger(__name__)

BATCH_SIZE = 5_000
MAX_BATCHES_PER_TABLE = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _delete_in_batches(
    session: AsyncSession,
    model: Any,
    *,
    cutoff_column,
    cutoff: datetime,
    extra_where=None,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Delete rows older than ``cutoff``, committing between batches."""
    deleted_total = 0
    for _ in range(MAX_BATCHES_PER_TABLE):
        subquery = select(model.id).where(cutoff_column < cutoff)
        if extra_where is not None:
            subquery = subquery.where(extra_where)
        subquery = subquery.limit(batch_size)

        ids = list((await session.execute(subquery)).scalars().all())
        if not ids:
            break

        await session.execute(delete(model).where(model.id.in_(ids)))
        await session.commit()
        deleted_total += len(ids)

        if len(ids) < batch_size:
            break
    return deleted_total


async def purge_monitoring_results(
    session: AsyncSession, *, retention_days: int
) -> int:
    cutoff = _now() - timedelta(days=retention_days)
    return await _delete_in_batches(
        session,
        MonitoringResult,
        cutoff_column=MonitoringResult.checked_at,
        cutoff=cutoff,
    )


async def purge_incidents(session: AsyncSession, *, retention_days: int) -> int:
    """Only resolved incidents are eligible - an open one is current state."""
    cutoff = _now() - timedelta(days=retention_days)
    return await _delete_in_batches(
        session,
        Incident,
        cutoff_column=Incident.started_at,
        cutoff=cutoff,
        extra_where=Incident.status == "resolved",
    )


async def purge_alerts(session: AsyncSession, *, retention_days: int) -> int:
    cutoff = _now() - timedelta(days=retention_days)
    return await _delete_in_batches(
        session,
        Alert,
        cutoff_column=Alert.created_at,
        cutoff=cutoff,
    )


async def purge_audit_logs(session: AsyncSession, *, retention_days: int) -> int:
    cutoff = _now() - timedelta(days=retention_days)
    return await _delete_in_batches(
        session,
        AuditLog,
        cutoff_column=AuditLog.created_at,
        cutoff=cutoff,
    )


async def purge_superseded_certificates(
    session: AsyncSession, *, retention_days: int
) -> int:
    """Drop rotated-away certificate observations; keep every current one."""
    cutoff = _now() - timedelta(days=retention_days)
    return await _delete_in_batches(
        session,
        SslCertificate,
        cutoff_column=SslCertificate.checked_at,
        cutoff=cutoff,
        extra_where=SslCertificate.is_current.is_(False),
    )


async def run_retention_sweep(
    session: AsyncSession, config: dict[str, Any]
) -> dict[str, int]:
    """Apply every retention policy. Safe to run concurrently on replicas."""
    started = _now()
    summary: dict[str, int] = {}

    try:
        summary["monitoring_results"] = await purge_monitoring_results(
            session, retention_days=int(config.get("data_retention_days", 90))
        )
        summary["superseded_certificates"] = await purge_superseded_certificates(
            session, retention_days=int(config.get("data_retention_days", 90))
        )
        summary["alerts"] = await purge_alerts(
            session, retention_days=int(config.get("alert_retention_days", 180))
        )
        summary["incidents"] = await purge_incidents(
            session, retention_days=int(config.get("incident_retention_days", 730))
        )
        summary["audit_logs"] = await purge_audit_logs(
            session, retention_days=int(config.get("audit_retention_days", 365))
        )
    except Exception as exc:
        await session.rollback()
        logger.error("retention_sweep_failed", error=str(exc))
        raise

    elapsed = (_now() - started).total_seconds()
    total = sum(summary.values())
    if total:
        logger.info("retention_sweep_completed", elapsed_seconds=round(elapsed, 2), **summary)
    else:
        logger.debug("retention_sweep_nothing_to_do", elapsed_seconds=round(elapsed, 2))
    return summary


async def storage_estimate(session: AsyncSession) -> dict[str, int]:
    """Row counts per historical table, shown on the settings page."""
    counts: dict[str, int] = {}
    for label, model in (
        ("monitoring_results", MonitoringResult),
        ("ssl_certificates", SslCertificate),
        ("incidents", Incident),
        ("alerts", Alert),
        ("audit_logs", AuditLog),
    ):
        counts[label] = int(
            (await session.execute(select(func.count(model.id)))).scalar() or 0
        )
    oldest = (
        await session.execute(select(func.min(MonitoringResult.checked_at)))
    ).scalar()
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        counts["oldest_result_age_days"] = max(
            0, int((_now() - oldest).total_seconds() // 86400)
        )
    return counts
