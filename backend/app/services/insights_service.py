"""Operational intelligence, computed locally.

Three things live here, and none of them calls anything outside this server:

* the **Smart DevOps summary** - what needs attention right now,
* the **daily operations summary** - what happened, and what it suggests,
* **infrastructure search** - a small deterministic parser that turns
  "production services that are down" into a database query.

The search parser is deliberately not a language model. It recognises a fixed
vocabulary of intents and filters and refuses anything it does not understand,
which has three properties an LLM cannot offer here: the query never leaves the
server, the same question always returns the same rows, and a question it
cannot parse produces an honest "I did not understand that" instead of a
confident wrong answer.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ChangeStatus,
    CheckStatus,
    EndpointStatus,
    IncidentStatus,
    OPEN_RCA_STATUSES,
    SslStatus,
)
from app.core.logging import get_logger
from app.models.change import Change
from app.models.endpoint import Endpoint, Environment
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult
from app.models.rca import Rca

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ==================================================== smart devops summary
async def smart_summary(session: AsyncSession) -> dict[str, Any]:
    """What needs attention, right now, in one call.

    Everything here is a count of real rows. The health score at the top is a
    weighted blend of four measured things, and its components are returned
    alongside it - a score without its reasons is a number nobody can act on.
    """
    day_ago = _now() - timedelta(hours=24)

    status_rows = (
        await session.execute(
            select(Endpoint.current_status, func.count(Endpoint.id))
            .where(
                Endpoint.monitoring_enabled.is_(True),
                Endpoint.is_paused.is_(False),
            )
            .group_by(Endpoint.current_status)
        )
    ).all()
    statuses = {str(s): int(c) for s, c in status_rows}
    monitored = sum(statuses.values())
    down = statuses.get(EndpointStatus.DOWN.value, 0)
    degraded = statuses.get(EndpointStatus.DEGRADED.value, 0)
    up = statuses.get(EndpointStatus.UP.value, 0)

    # Endpoints down in production carry the "critical" label; the same
    # failure elsewhere is serious but not an emergency.
    critical = int(
        (
            await session.execute(
                select(func.count(Endpoint.id))
                .join(Environment, Endpoint.environment_id == Environment.id)
                .where(
                    Endpoint.current_status == EndpointStatus.DOWN.value,
                    Endpoint.monitoring_enabled.is_(True),
                    Endpoint.is_paused.is_(False),
                    func.lower(Environment.name).in_(("production", "prod")),
                )
            )
        ).scalar()
        or 0
    )

    ssl_attention = int(
        (
            await session.execute(
                select(func.count(Endpoint.id)).where(
                    Endpoint.ssl_status.in_(
                        [SslStatus.EXPIRING_SOON.value, SslStatus.CRITICAL.value,
                         SslStatus.EXPIRED.value, SslStatus.INVALID.value]
                    ),
                    Endpoint.monitoring_enabled.is_(True),
                )
            )
        ).scalar()
        or 0
    )

    open_incidents = int(
        (
            await session.execute(
                select(func.count(Incident.id)).where(
                    Incident.status == IncidentStatus.OPEN.value
                )
            )
        ).scalar()
        or 0
    )

    recent_deployments = list(
        (
            await session.execute(
                select(Change)
                .where(
                    Change.completed_at.is_not(None),
                    Change.completed_at >= day_ago,
                )
                .order_by(Change.completed_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    active_deployments = int(
        (
            await session.execute(
                select(func.count(Change.id)).where(
                    Change.status == ChangeStatus.DEPLOYMENT_IN_PROGRESS.value
                )
            )
        ).scalar()
        or 0
    )

    # A deployment that finished shortly before an incident opened. Reported
    # as a correlation, and worded that way everywhere it surfaces.
    correlations = await _deployment_incident_correlations(session, since=day_ago)

    anomalies = await _performance_anomalies(session)

    rca_pending = int(
        (
            await session.execute(
                select(func.count(Rca.id)).where(
                    Rca.status.in_(list(OPEN_RCA_STATUSES))
                )
            )
        ).scalar()
        or 0
    )

    # ---- health score: four measured components, weighted and explained
    availability = round(up / monitored * 100, 1) if monitored else None
    components = {
        "availability": availability if availability is not None else 100.0,
        "ssl": round(
            max(0.0, 100.0 - (ssl_attention / monitored * 100 if monitored else 0)), 1
        ),
        "incidents": round(max(0.0, 100.0 - open_incidents * 10), 1),
        "deployments": round(max(0.0, 100.0 - len(correlations) * 15), 1),
    }
    weights = {"availability": 0.5, "ssl": 0.15, "incidents": 0.2, "deployments": 0.15}
    score = round(sum(components[k] * weights[k] for k in weights))

    reasons: list[str] = []
    if down:
        reasons.append(f"{down} endpoint(s) down")
    if degraded:
        reasons.append(f"{degraded} degraded")
    if ssl_attention:
        reasons.append(f"{ssl_attention} certificate(s) need attention")
    if open_incidents:
        reasons.append(f"{open_incidents} open incident(s)")
    if correlations:
        reasons.append(f"{len(correlations)} deployment/incident correlation(s)")
    if not reasons:
        reasons.append("no open incidents, no failing endpoints, no expiring certificates")

    return {
        "generated_at": _now(),
        "health_score": score,
        "health_components": components,
        "health_reasons": reasons,
        "monitored": monitored,
        "up": up,
        "down": down,
        "degraded": degraded,
        "critical_production_down": critical,
        "ssl_attention": ssl_attention,
        "open_incidents": open_incidents,
        "recent_deployments": len(recent_deployments),
        "active_deployments": active_deployments,
        "deployment_incident_correlations": correlations,
        "performance_anomalies": anomalies,
        "rca_pending": rca_pending,
        "attention": await _attention_list(session, correlations, anomalies),
    }


async def _deployment_incident_correlations(
    session: AsyncSession, *, since: datetime, window_minutes: int = 30
) -> list[dict[str, Any]]:
    """Incidents that opened shortly after a deployment finished.

    Timing only. The wording at every call site says correlation, because
    that is all this is - but it is the first thing worth looking at.
    """
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.started_at >= since)
                .order_by(Incident.started_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    if not incidents:
        return []

    from app.models.change import change_endpoints

    results: list[dict[str, Any]] = []
    for incident in incidents:
        change = (
            await session.execute(
                select(Change)
                .join(change_endpoints, change_endpoints.c.change_id == Change.id)
                .where(
                    change_endpoints.c.endpoint_id == incident.endpoint_id,
                    Change.completed_at.is_not(None),
                    Change.completed_at <= incident.started_at,
                    Change.completed_at
                    >= incident.started_at - timedelta(minutes=window_minutes),
                )
                .order_by(Change.completed_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if change is None:
            continue
        results.append({
            "incident_id": incident.id,
            "endpoint_id": str(incident.endpoint_id),
            "endpoint_name": getattr(incident.endpoint, "name", None),
            "incident_started_at": incident.started_at,
            "change_id": change.id,
            "change_reference": change.reference,
            "application": change.application,
            "completed_at": change.completed_at,
            "minutes_before_incident": round(
                (incident.started_at - change.completed_at).total_seconds() / 60, 1
            ),
        })
    return results


async def _performance_anomalies(
    session: AsyncSession, *, multiplier: float = 3.0, limit: int = 10
) -> list[dict[str, Any]]:
    """Endpoints answering correctly but much slower than their own normal.

    Compares the last hour against the preceding 24, per endpoint. An endpoint
    returning HTTP 200 five times slower than usual is invisible to a status
    check and is usually the first sign of what becomes an outage.
    """
    now = _now()
    recent_start = now - timedelta(hours=1)
    baseline_start = now - timedelta(hours=25)

    recent = (
        await session.execute(
            select(
                MonitoringResult.endpoint_id,
                func.avg(MonitoringResult.response_time_ms),
                func.count(MonitoringResult.id),
            )
            .where(
                MonitoringResult.checked_at >= recent_start,
                MonitoringResult.status != CheckStatus.DOWN.value,
                MonitoringResult.response_time_ms.is_not(None),
            )
            .group_by(MonitoringResult.endpoint_id)
        )
    ).all()
    baseline = (
        await session.execute(
            select(
                MonitoringResult.endpoint_id,
                func.avg(MonitoringResult.response_time_ms),
                func.count(MonitoringResult.id),
            )
            .where(
                MonitoringResult.checked_at >= baseline_start,
                MonitoringResult.checked_at < recent_start,
                MonitoringResult.status != CheckStatus.DOWN.value,
                MonitoringResult.response_time_ms.is_not(None),
            )
            .group_by(MonitoringResult.endpoint_id)
        )
    ).all()

    baseline_map = {
        row[0]: (float(row[1]), int(row[2])) for row in baseline if row[1]
    }
    findings: list[dict[str, Any]] = []
    for endpoint_id, current, samples in recent:
        if not current or samples < 3:
            continue
        base = baseline_map.get(endpoint_id)
        # Needs a real baseline to compare against; too few samples and the
        # "anomaly" is just noise.
        if not base or base[1] < 10:
            continue
        base_ms, _count = base
        if base_ms <= 0:
            continue
        ratio = float(current) / base_ms
        if ratio < multiplier:
            continue
        findings.append({
            "endpoint_id": str(endpoint_id),
            "current_ms": round(float(current), 1),
            "baseline_ms": round(base_ms, 1),
            "ratio": round(ratio, 2),
            "increase_percent": round((ratio - 1) * 100, 1),
        })

    findings.sort(key=lambda item: item["ratio"], reverse=True)
    findings = findings[:limit]

    if findings:
        rows = (
            await session.execute(
                select(Endpoint.id, Endpoint.name, Endpoint.url, Endpoint.application)
                .where(Endpoint.id.in_([f["endpoint_id"] for f in findings]))
            )
        ).all()
        names = {str(r[0]): {"name": r[1], "url": r[2], "application": r[3]} for r in rows}
        for finding in findings:
            finding.update(names.get(finding["endpoint_id"], {}))
    return findings


async def _attention_list(
    session: AsyncSession,
    correlations: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The prioritised "look at these" list.

    Ordered by environment, then by kind of failure, then by how long it has
    been going on - which is the order an engineer would pick themselves.
    """
    items: list[dict[str, Any]] = []
    correlated_endpoints = {c["endpoint_id"] for c in correlations}

    failing = list(
        (
            await session.execute(
                select(Endpoint)
                .where(
                    Endpoint.current_status.in_(
                        [EndpointStatus.DOWN.value, EndpointStatus.DEGRADED.value]
                    ),
                    Endpoint.monitoring_enabled.is_(True),
                    Endpoint.is_paused.is_(False),
                )
                .limit(50)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    for endpoint in failing:
        environment = endpoint.environment.name if endpoint.environment else None
        production = (environment or "").lower() in ("production", "prod")
        is_down = endpoint.current_status == EndpointStatus.DOWN.value
        correlated = str(endpoint.id) in correlated_endpoints

        if production and is_down:
            priority = "critical"
        elif is_down:
            priority = "high"
        elif production:
            priority = "high"
        else:
            priority = "medium"

        detail = (
            f"{'Down' if is_down else 'Degraded'}"
            + (f" in {environment}" if environment else "")
            + (f" - {endpoint.last_error}" if endpoint.last_error else "")
        )
        if correlated:
            match = next(
                c for c in correlations if c["endpoint_id"] == str(endpoint.id)
            )
            detail += (
                f". Deployment {match['change_reference']} completed "
                f"{match['minutes_before_incident']:.0f} minutes before it started "
                "(correlation, not confirmed cause)."
            )

        items.append({
            "priority": priority,
            "kind": "endpoint_down" if is_down else "endpoint_degraded",
            "title": endpoint.name,
            "detail": detail,
            "endpoint_id": str(endpoint.id),
            "application": endpoint.application,
            "environment": environment,
            "change_reference": (
                next(
                    (c["change_reference"] for c in correlations
                     if c["endpoint_id"] == str(endpoint.id)),
                    None,
                )
            ),
        })

    for anomaly in anomalies[:5]:
        items.append({
            "priority": "medium",
            "kind": "performance_anomaly",
            "title": anomaly.get("name") or "Endpoint",
            "detail": (
                f"Responding at {anomaly['current_ms']:.0f} ms against a "
                f"{anomaly['baseline_ms']:.0f} ms baseline "
                f"(+{anomaly['increase_percent']:.0f}%). Still returning a "
                "correct response."
            ),
            "endpoint_id": anomaly["endpoint_id"],
            "application": anomaly.get("application"),
            "environment": None,
            "change_reference": None,
        })

    expiring = list(
        (
            await session.execute(
                select(Endpoint)
                .where(
                    Endpoint.ssl_status.in_(
                        [SslStatus.CRITICAL.value, SslStatus.EXPIRED.value]
                    ),
                    Endpoint.monitoring_enabled.is_(True),
                )
                .limit(10)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    for endpoint in expiring:
        expired = endpoint.ssl_status == SslStatus.EXPIRED.value
        items.append({
            "priority": "high" if expired else "medium",
            "kind": "ssl",
            "title": endpoint.name,
            "detail": (
                "Certificate has expired"
                if expired
                else f"Certificate expires in {endpoint.ssl_days_remaining} day(s)"
            ),
            "endpoint_id": str(endpoint.id),
            "application": endpoint.application,
            "environment": (
                endpoint.environment.name if endpoint.environment else None
            ),
            "change_reference": None,
        })

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items.sort(key=lambda item: order.get(item["priority"], 9))
    return items[:15]


# ===================================================== daily operations
async def daily_summary(
    session: AsyncSession, *, hours: int = 24
) -> dict[str, Any]:
    """What happened in the last day, and the one thing it suggests."""
    since = _now() - timedelta(hours=hours)

    monitored = int(
        (
            await session.execute(
                select(func.count(Endpoint.id)).where(
                    Endpoint.monitoring_enabled.is_(True)
                )
            )
        ).scalar()
        or 0
    )

    incidents = list(
        (
            await session.execute(
                select(Incident).where(Incident.started_at >= since)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    resolved = [i for i in incidents if i.status == IncidentStatus.RESOLVED.value]

    endpoints_with_incidents = {i.endpoint_id for i in incidents}
    healthy_throughout = max(0, monitored - len(endpoints_with_incidents))

    deployments = list(
        (
            await session.execute(
                select(Change).where(
                    Change.completed_at.is_not(None),
                    Change.completed_at >= since,
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    failed_deployments = [
        c for c in deployments if c.status == ChangeStatus.FAILED.value
    ]

    ssl_issues = int(
        (
            await session.execute(
                select(func.count(Endpoint.id)).where(
                    Endpoint.ssl_status.in_(
                        [SslStatus.CRITICAL.value, SslStatus.EXPIRED.value]
                    )
                )
            )
        ).scalar()
        or 0
    )

    rca_pending = int(
        (
            await session.execute(
                select(func.count(Rca.id)).where(
                    Rca.status.in_(list(OPEN_RCA_STATUSES))
                )
            )
        ).scalar()
        or 0
    )

    correlations = await _deployment_incident_correlations(session, since=since)

    # ---- the single finding worth leading with, if there is one
    findings: list[str] = []
    recommendations: list[str] = []

    if correlations:
        apps = Counter(c["application"] for c in correlations if c["application"])
        if apps:
            app, count = apps.most_common(1)[0]
            findings.append(
                f"{app} had {count} incident(s) begin shortly after a deployment."
            )
            recommendations.append(
                "Review post-deployment health validation for this application."
            )

    endpoint_counts = Counter(i.endpoint_id for i in incidents)
    for endpoint_id, count in endpoint_counts.most_common(1):
        if count >= 3:
            row = (
                await session.execute(
                    select(Endpoint.name).where(Endpoint.id == endpoint_id)
                )
            ).scalar()
            findings.append(
                f"{row or 'One endpoint'} opened {count} separate incidents - "
                "that pattern is usually one unresolved problem, not several."
            )
            recommendations.append(
                "Investigate the underlying instability rather than each occurrence."
            )

    if failed_deployments:
        findings.append(
            f"{len(failed_deployments)} deployment(s) were marked failed."
        )
    if ssl_issues:
        recommendations.append(
            f"Renew {ssl_issues} certificate(s) that are expired or expiring within days."
        )
    if rca_pending >= 5:
        recommendations.append(
            f"{rca_pending} RCAs are open - the backlog is where recurring "
            "problems hide."
        )

    return {
        "generated_at": _now(),
        "window_hours": hours,
        "endpoints_monitored": monitored,
        "endpoints_healthy_throughout": healthy_throughout,
        "incidents": len(incidents),
        "incidents_resolved": len(resolved),
        "deployments": len(deployments),
        "deployments_failed": len(failed_deployments),
        "ssl_issues": ssl_issues,
        "rca_pending": rca_pending,
        "deployment_incident_correlations": len(correlations),
        "findings": findings,
        "recommendations": recommendations,
    }


# ====================================================== local infra search
#
# A small, explicit intent parser. It matches a fixed vocabulary and says so
# when it cannot - the honest failure mode. Nothing typed here leaves the
# server, and the same question always returns the same rows.

_NUMBER = re.compile(r"(\d+)")


def _extract_days(text: str, default: int) -> int:
    match = _NUMBER.search(text)
    return int(match.group(1)) if match else default


def _extract_ms(text: str) -> float | None:
    """Pull a latency threshold out of phrases like 'above 1 second' or '>500ms'."""
    seconds = re.search(r"(\d+(?:\.\d+)?)\s*(?:second|sec|s)\b", text)
    if seconds:
        return float(seconds.group(1)) * 1000
    millis = re.search(r"(\d+(?:\.\d+)?)\s*(?:millisecond|ms)\b", text)
    if millis:
        return float(millis.group(1))
    return None


async def search(session: AsyncSession, query: str) -> dict[str, Any]:
    """Answer a plain-language question from the local database.

    Returns the matched intent alongside the rows, so the UI can show *what it
    understood* - which is what makes a wrong interpretation obvious rather
    than silently misleading.
    """
    text = (query or "").strip().lower()
    if not text:
        return _empty("Type a question, for example: production services that are down")

    production = any(word in text for word in ("production", "prod"))

    # ---- SSL expiry
    if "ssl" in text or "certificate" in text or "cert " in text:
        days = _extract_days(text, 30)
        return await _ssl_expiring(session, days=days, production=production)

    # ---- paused endpoints
    if "paused" in text:
        return await _paused(session)

    # ---- latency
    if any(word in text for word in ("latency", "slow", "response time", "slowest")):
        threshold = _extract_ms(text)
        return await _slow_endpoints(session, threshold_ms=threshold, production=production)

    # ---- failed deployments
    if "deployment" in text or "deploy" in text:
        failed = "fail" in text
        days = 7 if "week" in text else 30 if "month" in text else 1 if "today" in text else 7
        return await _deployments(session, failed_only=failed, days=days)

    # ---- RCA
    if "rca" in text or "root cause" in text:
        if "without" in text or "no rca" in text or "missing" in text:
            return await _incidents_without_rca(session)
        days = _extract_days(text, 7) if "more than" in text or "older" in text else None
        return await _rca_pending(session, older_than_days=days)

    # ---- recurring incidents
    if "recurring" in text or "most incidents" in text or "repeat" in text:
        return await _recurring_incidents(session)

    # ---- incidents
    if "incident" in text:
        days = 7 if "week" in text else 30 if "month" in text else 1
        return await _incidents(session, days=days)

    # ---- down / unhealthy / degraded
    if any(word in text for word in ("down", "unhealthy", "failing", "broken", "degraded")):
        return await _unhealthy(session, production=production, include_degraded=True)

    return _empty(
        "That question was not recognised. Try: production services that are down; "
        "SSL certificates expiring in 30 days; endpoints with latency above 1 second; "
        "failed deployments this week; incidents without RCA; currently paused endpoints."
    )


def _empty(message: str) -> dict[str, Any]:
    return {
        "understood": False,
        "intent": None,
        "description": message,
        "count": 0,
        "columns": [],
        "rows": [],
    }


def _result(
    intent: str, description: str, columns: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "understood": True,
        "intent": intent,
        "description": description,
        "count": len(rows),
        "columns": columns,
        "rows": rows,
    }


def _endpoint_scope(stmt: Select, production: bool) -> Select:
    if production:
        stmt = stmt.join(Environment, Endpoint.environment_id == Environment.id).where(
            func.lower(Environment.name).in_(("production", "prod"))
        )
    return stmt


async def _unhealthy(
    session: AsyncSession, *, production: bool, include_degraded: bool
) -> dict[str, Any]:
    statuses = [EndpointStatus.DOWN.value]
    if include_degraded:
        statuses.append(EndpointStatus.DEGRADED.value)

    stmt = select(Endpoint).where(
        Endpoint.current_status.in_(statuses),
        Endpoint.monitoring_enabled.is_(True),
        Endpoint.is_paused.is_(False),
    )
    stmt = _endpoint_scope(stmt, production)
    rows = list((await session.execute(stmt.limit(100))).scalars().unique().all())

    return _result(
        "unhealthy_endpoints",
        f"{'Production endpoints' if production else 'Endpoints'} currently down or degraded",
        ["Endpoint", "Status", "Application", "Environment", "Last error"],
        [
            {
                "id": str(e.id),
                "link": f"/endpoints/{e.id}",
                "Endpoint": e.name,
                "Status": e.current_status,
                "Application": e.application or "—",
                "Environment": e.environment.name if e.environment else "—",
                "Last error": (e.last_error or "—")[:120],
            }
            for e in rows
        ],
    )


async def _slow_endpoints(
    session: AsyncSession, *, threshold_ms: float | None, production: bool
) -> dict[str, Any]:
    threshold = threshold_ms or 1000.0
    stmt = select(Endpoint).where(
        Endpoint.last_response_time_ms.is_not(None),
        Endpoint.last_response_time_ms >= threshold,
        Endpoint.monitoring_enabled.is_(True),
    )
    stmt = _endpoint_scope(stmt, production)
    rows = list(
        (
            await session.execute(
                stmt.order_by(Endpoint.last_response_time_ms.desc()).limit(50)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "slow_endpoints",
        f"Endpoints whose last response took {threshold:.0f} ms or more",
        ["Endpoint", "Response time", "Status", "Application"],
        [
            {
                "id": str(e.id),
                "link": f"/endpoints/{e.id}",
                "Endpoint": e.name,
                "Response time": f"{e.last_response_time_ms:.0f} ms",
                "Status": e.current_status,
                "Application": e.application or "—",
            }
            for e in rows
        ],
    )


async def _ssl_expiring(
    session: AsyncSession, *, days: int, production: bool
) -> dict[str, Any]:
    stmt = select(Endpoint).where(
        Endpoint.ssl_days_remaining.is_not(None),
        Endpoint.ssl_days_remaining <= days,
        Endpoint.ssl_monitoring_enabled.is_(True),
    )
    stmt = _endpoint_scope(stmt, production)
    rows = list(
        (
            await session.execute(
                stmt.order_by(Endpoint.ssl_days_remaining.asc()).limit(100)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "ssl_expiring",
        f"Certificates expiring within {days} days",
        ["Endpoint", "Days remaining", "SSL status", "Environment"],
        [
            {
                "id": str(e.id),
                "link": f"/endpoints/{e.id}",
                "Endpoint": e.name,
                "Days remaining": e.ssl_days_remaining,
                "SSL status": e.ssl_status,
                "Environment": e.environment.name if e.environment else "—",
            }
            for e in rows
        ],
    )


async def _paused(session: AsyncSession) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Endpoint).where(Endpoint.is_paused.is_(True)).limit(100)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "paused_endpoints",
        "Endpoints with monitoring currently paused",
        ["Endpoint", "Reason", "Application", "Environment"],
        [
            {
                "id": str(e.id),
                "link": f"/endpoints/{e.id}",
                "Endpoint": e.name,
                "Reason": e.pause_reason or "Paused manually",
                "Application": e.application or "—",
                "Environment": e.environment.name if e.environment else "—",
            }
            for e in rows
        ],
    )


async def _deployments(
    session: AsyncSession, *, failed_only: bool, days: int
) -> dict[str, Any]:
    since = _now() - timedelta(days=days)
    stmt = select(Change).where(Change.started_at.is_not(None), Change.started_at >= since)
    if failed_only:
        stmt = stmt.where(Change.status == ChangeStatus.FAILED.value)
    rows = list(
        (await session.execute(stmt.order_by(Change.started_at.desc()).limit(100)))
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "deployments",
        f"{'Failed deployments' if failed_only else 'Deployments'} in the last {days} day(s)",
        ["Change", "Application", "Environment", "Status", "Deployer", "Started"],
        [
            {
                "id": str(c.id),
                "link": f"/changes/{c.id}",
                "Change": c.reference,
                "Application": c.application,
                "Environment": c.environment_name or "—",
                "Status": c.status,
                "Deployer": c.deployer_name or "—",
                "Started": c.started_at.strftime("%Y-%m-%d %H:%M") if c.started_at else "—",
            }
            for c in rows
        ],
    )


async def _incidents(session: AsyncSession, *, days: int) -> dict[str, Any]:
    since = _now() - timedelta(days=days)
    rows = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.started_at >= since)
                .order_by(Incident.started_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "incidents",
        f"Incidents in the last {days} day(s)",
        ["Incident", "Endpoint", "Status", "Reason", "Started"],
        [
            {
                "id": str(i.id),
                "link": "/incidents",
                "Incident": f"INC-{i.id}",
                "Endpoint": getattr(i.endpoint, "name", "—"),
                "Status": i.status,
                "Reason": i.reason or "—",
                "Started": i.started_at.strftime("%Y-%m-%d %H:%M"),
            }
            for i in rows
        ],
    )


async def _incidents_without_rca(session: AsyncSession) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.id.notin_(select(Rca.incident_id)))
                .order_by(Incident.started_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return _result(
        "incidents_without_rca",
        "Incidents with no RCA record - nobody has decided whether one is needed",
        ["Incident", "Endpoint", "Status", "Started"],
        [
            {
                "id": str(i.id),
                "link": "/rca",
                "Incident": f"INC-{i.id}",
                "Endpoint": getattr(i.endpoint, "name", "—"),
                "Status": i.status,
                "Started": i.started_at.strftime("%Y-%m-%d %H:%M"),
            }
            for i in rows
        ],
    )


async def _rca_pending(
    session: AsyncSession, *, older_than_days: int | None
) -> dict[str, Any]:
    stmt = select(Rca).where(Rca.status.in_(list(OPEN_RCA_STATUSES)))
    label = "RCAs currently open"
    if older_than_days:
        stmt = stmt.where(Rca.created_at <= _now() - timedelta(days=older_than_days))
        label = f"RCAs open for more than {older_than_days} days"
    rows = list(
        (await session.execute(stmt.order_by(Rca.created_at.asc()).limit(100)))
        .scalars()
        .all()
    )
    return _result(
        "rca_pending",
        label,
        ["RCA", "Endpoint", "Owner", "Status", "Age (days)"],
        [
            {
                "id": str(r.id),
                "link": f"/rca/{r.id}",
                "RCA": f"RCA-{r.id}",
                "Endpoint": r.endpoint_name or "—",
                "Owner": r.owner_label or "Unassigned",
                "Status": r.status,
                "Age (days)": r.age_days,
            }
            for r in rows
        ],
    )


async def _recurring_incidents(session: AsyncSession) -> dict[str, Any]:
    since = _now() - timedelta(days=30)
    rows = (
        await session.execute(
            select(
                Incident.endpoint_id,
                func.count(Incident.id).label("total"),
            )
            .where(Incident.started_at >= since)
            .group_by(Incident.endpoint_id)
            .having(func.count(Incident.id) >= 2)
            .order_by(func.count(Incident.id).desc())
            .limit(25)
        )
    ).all()
    if not rows:
        return _result(
            "recurring_incidents",
            "No endpoint has had more than one incident in the last 30 days",
            ["Endpoint", "Incidents"],
            [],
        )

    names = {
        r[0]: (r[1], r[2])
        for r in (
            await session.execute(
                select(Endpoint.id, Endpoint.name, Endpoint.application).where(
                    Endpoint.id.in_([row[0] for row in rows])
                )
            )
        ).all()
    }
    return _result(
        "recurring_incidents",
        "Endpoints with repeated incidents in the last 30 days",
        ["Endpoint", "Application", "Incidents"],
        [
            {
                "id": str(endpoint_id),
                "link": f"/endpoints/{endpoint_id}",
                "Endpoint": names.get(endpoint_id, ("—", None))[0],
                "Application": names.get(endpoint_id, (None, "—"))[1] or "—",
                "Incidents": total,
            }
            for endpoint_id, total in rows
        ],
    )
