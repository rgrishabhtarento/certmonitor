"""Local RCA draft and timeline generation.

Nothing here calls anything. The draft is assembled from rows this
application already holds - the incident, the diagnosis the engine produced at
the time, the change that was deploying, the monitoring results either side of
the failure, and what people wrote in the comments.

That constraint is the point rather than a limitation. A draft stitched from
real records can be checked line by line against the evidence beside it, and
it cannot invent a cause that nobody observed. Where a fact is missing it says
so, in the same words every time:

    Not available from monitoring data.

The output is explicitly a *draft*. The owner edits it before saving, because
the one thing this cannot know is what the engineer actually found when they
looked - and that is the part worth writing down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import CheckStatus, RootCauseCategory
from app.core.logging import get_logger
from app.models.change import Change, change_endpoints
from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult
from app.models.rca import IncidentComment

logger = get_logger(__name__)

UNAVAILABLE = "Not available from monitoring data."

# How far either side of the incident to look for events worth putting on the
# timeline. Wide enough to catch the deployment that preceded it, narrow
# enough that unrelated activity does not crowd it out.
TIMELINE_LOOKBACK_MINUTES = 120
TIMELINE_LOOKAHEAD_MINUTES = 120

# Which diagnosis verdicts imply which root-cause category. Only a mapping
# from something already concluded - it never guesses a category from nothing.
VERDICT_CATEGORY = {
    "dns_failure": RootCauseCategory.NETWORK.value,
    "connection_refused": RootCauseCategory.INFRASTRUCTURE.value,
    "connection_timeout": RootCauseCategory.NETWORK.value,
    "partial_backend": RootCauseCategory.INFRASTRUCTURE.value,
    "cert_expired": RootCauseCategory.SSL_TLS.value,
    "cert_hostname_mismatch": RootCauseCategory.SSL_TLS.value,
    "cert_chain_incomplete": RootCauseCategory.SSL_TLS.value,
    "cert_self_signed": RootCauseCategory.SSL_TLS.value,
    "tls_failure": RootCauseCategory.SSL_TLS.value,
    "upstream_unavailable": RootCauseCategory.APPLICATION.value,
    "application_error": RootCauseCategory.APPLICATION.value,
    "http_no_response": RootCauseCategory.APPLICATION.value,
    "wrong_path": RootCauseCategory.CONFIGURATION.value,
    "likely_wrong_path_or_expectation": RootCauseCategory.CONFIGURATION.value,
    "auth_required": RootCauseCategory.CONFIGURATION.value,
    "http_status_mismatch": RootCauseCategory.CONFIGURATION.value,
    "unexpected_redirect": RootCauseCategory.CONFIGURATION.value,
    "rate_limited": RootCauseCategory.EXTERNAL_DEPENDENCY.value,
    "intermittent_failure": RootCauseCategory.INFRASTRUCTURE.value,
    "performance_degradation": RootCauseCategory.APPLICATION.value,
    "configuration_suspect": RootCauseCategory.CONFIGURATION.value,
}


def _entry(at: datetime | None, kind: str, detail: str, source: str) -> dict[str, Any]:
    return {
        "at": at.isoformat() if at else None,
        "kind": kind,
        "detail": detail,
        "source": source,
    }


def _minutes(seconds: int | None) -> str:
    if not seconds:
        return UNAVAILABLE
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}h {rest:02d}m"


# ------------------------------------------------------------- evidence
async def gather_evidence(
    session: AsyncSession, incident: Incident
) -> dict[str, Any]:
    """Everything the application knows that bears on this incident.

    Read-only, and every field is either a real value or absent - there is no
    placeholder that could be mistaken for a measurement.
    """
    endpoint = incident.endpoint
    started = incident.started_at
    window_start = started - timedelta(minutes=TIMELINE_LOOKBACK_MINUTES)
    window_end = (incident.resolved_at or datetime.now(timezone.utc)) + timedelta(
        minutes=TIMELINE_LOOKAHEAD_MINUTES
    )

    # ---- the diagnosis closest to the incident, if one was ever run
    diagnosis = (
        await session.execute(
            select(Diagnosis)
            .where(
                Diagnosis.endpoint_id == incident.endpoint_id,
                Diagnosis.created_at >= window_start,
                Diagnosis.created_at <= window_end,
            )
            .order_by(Diagnosis.created_at.asc())
            .limit(1)
        )
    ).scalars().first()

    # ---- deployments overlapping the window, for this endpoint or its app
    change_stmt = (
        select(Change)
        .where(
            Change.started_at.is_not(None),
            Change.started_at <= window_end,
        )
        .order_by(Change.started_at.desc())
        .limit(5)
    )
    change_stmt = change_stmt.where(
        Change.id.in_(
            select(change_endpoints.c.change_id).where(
                change_endpoints.c.endpoint_id == incident.endpoint_id
            )
        )
    )
    changes = list((await session.execute(change_stmt)).scalars().unique().all())
    # The one that finished most recently before the incident began is the
    # only one with a timing relationship worth reporting.
    preceding = next(
        (
            c
            for c in changes
            if c.completed_at and c.completed_at <= started
        ),
        None,
    )
    gap_minutes = (
        round((started - preceding.completed_at).total_seconds() / 60, 1)
        if preceding and preceding.completed_at
        else None
    )

    # ---- monitoring either side of the incident
    before = (
        await session.execute(
            select(
                MonitoringResult.status,
                MonitoringResult.response_time_ms,
                MonitoringResult.http_status_code,
            )
            .where(
                MonitoringResult.endpoint_id == incident.endpoint_id,
                MonitoringResult.checked_at < started,
                MonitoringResult.checked_at >= window_start,
            )
            .order_by(MonitoringResult.checked_at.desc())
            .limit(50)
        )
    ).all()
    during = (
        await session.execute(
            select(
                MonitoringResult.status,
                MonitoringResult.http_status_code,
                MonitoringResult.failure_reason,
                MonitoringResult.error_message,
                MonitoringResult.response_time_ms,
            )
            .where(
                MonitoringResult.endpoint_id == incident.endpoint_id,
                MonitoringResult.checked_at >= started,
                MonitoringResult.checked_at <= (incident.resolved_at or window_end),
            )
            .order_by(MonitoringResult.checked_at.asc())
            .limit(200)
        )
    ).all()

    healthy_before = [
        float(ms) for status, ms, _c in before
        if status != CheckStatus.DOWN.value and ms is not None
    ]
    baseline_ms = (
        round(sorted(healthy_before)[len(healthy_before) // 2], 1)
        if healthy_before
        else None
    )
    codes_during = [c for _s, c, _r, _e, _ms in during if c is not None]
    reasons_during = [r for _s, _c, r, _e, _ms in during if r]
    errors_during = [e for _s, _c, _r, e, _ms in during if e]

    comments = list(
        (
            await session.execute(
                select(IncidentComment)
                .where(IncidentComment.incident_id == incident.id)
                .order_by(IncidentComment.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    # ---- previous incidents on this endpoint with the same failure reason
    similar = (
        await session.execute(
            select(Incident.id, Incident.started_at, Incident.reason)
            .where(
                Incident.endpoint_id == incident.endpoint_id,
                Incident.id != incident.id,
                Incident.started_at >= started - timedelta(days=90),
            )
            .order_by(Incident.started_at.desc())
            .limit(20)
        )
    ).all()
    same_reason = [row for row in similar if row[2] == incident.reason]

    return {
        "incident": incident,
        "endpoint": endpoint,
        "diagnosis": diagnosis,
        "changes": changes,
        "preceding_change": preceding,
        "change_gap_minutes": gap_minutes,
        "baseline_response_time_ms": baseline_ms,
        "http_codes_during": sorted(set(codes_during)),
        "failure_reasons_during": sorted(set(reasons_during)),
        "first_error": errors_during[0] if errors_during else None,
        "failed_checks": sum(
            1 for status, _c, _r, _e, _ms in during
            if status == CheckStatus.DOWN.value
        ),
        "comments": comments,
        "similar_incidents": len(similar),
        "similar_same_reason": len(same_reason),
    }


# ------------------------------------------------------------- timeline
def build_timeline(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Assemble a timeline from events that actually happened.

    Every entry names its source, so the owner can tell an automatically
    derived fact from something they typed. Entries are editable afterwards -
    the parts a human remembers (when the rollback was decided, who was
    called) are exactly the ones no database holds.
    """
    incident: Incident = evidence["incident"]
    entries: list[dict[str, Any]] = []

    for change in evidence["changes"]:
        if change.started_at:
            entries.append(_entry(
                change.started_at, "deployment_started",
                f"{change.reference} deployment started by "
                f"{change.deployer_name or 'unknown'} ({change.application})",
                "change",
            ))
        if change.completed_at:
            entries.append(_entry(
                change.completed_at,
                "deployment_completed" if change.status == "completed" else "deployment_failed",
                f"{change.reference} {'completed' if change.status == 'completed' else 'marked failed'}",
                "change",
            ))

    entries.append(_entry(
        incident.started_at, "incident_started",
        f"Endpoint became unhealthy"
        + (f" - {incident.error_message}" if incident.error_message else ""),
        "monitoring",
    ))

    diagnosis = evidence.get("diagnosis")
    if diagnosis is not None:
        entries.append(_entry(
            diagnosis.created_at, "diagnosis",
            f"Smart Diagnose: {diagnosis.headline} "
            f"({diagnosis.confidence} confidence)",
            "diagnosis",
        ))

    for entry in (incident.timeline or []):
        at = entry.get("at")
        parsed = None
        if isinstance(at, str):
            try:
                parsed = datetime.fromisoformat(at)
            except ValueError:
                parsed = None
        elif isinstance(at, datetime):
            parsed = at
        entries.append(_entry(
            parsed, entry.get("kind") or "note",
            entry.get("detail") or "", "incident",
        ))

    for comment in evidence["comments"]:
        entries.append(_entry(
            comment.created_at, "comment",
            f"{comment.username or 'unknown'}: {comment.body}",
            "comment",
        ))

    if incident.resolved_at:
        entries.append(_entry(
            incident.resolved_at, "incident_resolved",
            "Endpoint recovered and the incident closed automatically"
            + (
                f" (HTTP {incident.recovery_status_code})"
                if incident.recovery_status_code
                else ""
            ),
            "monitoring",
        ))

    # Undated entries sort last rather than crashing the comparison.
    return sorted(entries, key=lambda e: (e["at"] is None, e["at"] or ""))


# ---------------------------------------------------------------- draft
def build_draft(evidence: dict[str, Any]) -> dict[str, Any]:
    """Compose the four RCA fields from the evidence.

    Written to be *edited*. Each section states what was observed and stops:
    where the data does not support a conclusion it says so rather than
    reaching for a plausible one, because a confident wrong root cause in a
    saved RCA outlives the incident and misleads whoever reads it next.
    """
    incident: Incident = evidence["incident"]
    endpoint = evidence["endpoint"]
    diagnosis = evidence.get("diagnosis")
    preceding = evidence.get("preceding_change")
    gap = evidence.get("change_gap_minutes")

    name = getattr(endpoint, "name", None) or "The endpoint"
    application = getattr(endpoint, "application", None)
    environment = (
        endpoint.environment.name
        if getattr(endpoint, "environment", None)
        else None
    )

    # ---------------------------------------------------------- root cause
    if diagnosis is not None:
        root_cause = (
            f"{diagnosis.root_cause or diagnosis.headline}\n\n"
            f"Smart Diagnose reached this with {diagnosis.confidence} confidence "
            f"at {diagnosis.created_at:%Y-%m-%d %H:%M} UTC"
        )
        if diagnosis.deepest_layer_ok:
            root_cause += (
                f", having confirmed the request reached the "
                f"{diagnosis.deepest_layer_ok.upper()} layer successfully"
            )
        root_cause += "."
        category = VERDICT_CATEGORY.get(diagnosis.verdict)
    else:
        codes = evidence["http_codes_during"]
        reasons = evidence["failure_reasons_during"]
        parts = []
        if codes:
            parts.append(
                "returned HTTP " + ", ".join(str(c) for c in codes[:5])
            )
        if reasons:
            parts.append("failure reason " + ", ".join(reasons[:3]))
        if parts:
            root_cause = (
                f"{name} {' and '.join(parts)} during the incident.\n\n"
                "No diagnosis was run while the incident was open, so the "
                "underlying cause was not established at the time. "
                + UNAVAILABLE
            )
        else:
            root_cause = UNAVAILABLE
        category = None

    if preceding and gap is not None:
        root_cause += (
            f"\n\nDeployment {preceding.reference} completed {gap:.0f} minutes "
            f"before the incident began. This is a correlation in time and does "
            f"not by itself establish that the deployment caused the failure."
        )
        if category is None:
            category = RootCauseCategory.DEPLOYMENT.value

    # -------------------------------------------------------------- impact
    duration = _minutes(incident.duration_seconds)
    impact_bits = [
        f"{name}"
        + (f" ({application})" if application else "")
        + (f" in {environment}" if environment else "")
        + " was unavailable"
    ]
    if incident.duration_seconds:
        impact_bits.append(f"for {duration}")
    impact_bits.append(
        f"from {incident.started_at:%Y-%m-%d %H:%M} UTC"
        + (
            f" until {incident.resolved_at:%H:%M} UTC"
            if incident.resolved_at
            else " and has not yet recovered"
        )
    )
    impact = " ".join(impact_bits) + "."
    if evidence["failed_checks"]:
        impact += (
            f" {evidence['failed_checks']} consecutive monitoring checks failed."
        )
    impact += (
        "\n\nUser-facing impact is not measured by CertMonitor - it observes "
        "the endpoint, not the traffic through it. Add what was actually "
        "affected."
    )

    # ---------------------------------------------------------- resolution
    if incident.resolved_at:
        resolution = (
            f"The endpoint returned to a healthy state at "
            f"{incident.resolved_at:%Y-%m-%d %H:%M} UTC"
            + (
                f" (HTTP {incident.recovery_status_code}"
                + (
                    f", {incident.recovery_response_time_ms:.0f} ms"
                    if incident.recovery_response_time_ms
                    else ""
                )
                + ")"
                if incident.recovery_status_code
                else ""
            )
            + " and the incident closed automatically.\n\n"
            "What was actually done to fix it is not recorded in monitoring "
            "data. Describe the remediation here."
        )
    else:
        resolution = "The incident is still open. " + UNAVAILABLE

    # -------------------------------------------------- preventive actions
    actions: list[dict[str, Any]] = []
    if preceding:
        actions.append({
            "text": "Review post-deployment health validation for "
                    f"{preceding.application}",
            "done": False,
        })
    if evidence["similar_same_reason"] >= 2:
        actions.append({
            "text": (
                f"Investigate why this endpoint has failed for the same reason "
                f"{evidence['similar_same_reason']} times in 90 days"
            ),
            "done": False,
        })
    if diagnosis is not None and diagnosis.verdict.startswith("cert_"):
        actions.append({
            "text": "Automate certificate renewal for this hostname",
            "done": False,
        })
    if evidence["baseline_response_time_ms"] is None:
        actions.append({
            "text": (
                "Ensure this endpoint has enough monitoring history to "
                "establish a latency baseline"
            ),
            "done": False,
        })

    return {
        "root_cause": root_cause,
        "root_cause_category": category,
        "impact": impact,
        "resolution": resolution,
        "preventive_actions": actions,
        "diagnosis_id": diagnosis.id if diagnosis is not None else None,
        "change_id": preceding.id if preceding is not None else None,
        "notice": (
            "Generated from available monitoring and incident data. "
            "Review before saving."
        ),
    }
