"""Persistence and state transitions for check results.

This is the only place that turns a :class:`CheckOutcome` into database state.
It owns:

* writing the ``monitoring_results`` row,
* the endpoint status machine (UNKNOWN -> UP -> DOWN -> RECOVERED -> UP),
* opening exactly one incident per continuous outage and closing it on
  recovery,
* certificate history, and
* alert generation.

Keeping it separate from :mod:`app.monitoring.checker` means the API's "test
this endpoint now" can run a real probe without writing anything, and the
worker can reuse the identical logic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AlertType,
    CheckStatus,
    EndpointStatus,
    FailureReason,
    IncidentStatus,
    Severity,
    SslStatus,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.monitoring.checker import (
    CheckOutcome,
    build_target_from_endpoint,
    run_check,
)
from app.monitoring.ssl_inspect import CertificateInfo, classify_certificate
from app.models.endpoint import Endpoint
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate
from app.services import alert_service

logger = get_logger(__name__)

_HUMAN_REASONS = {
    FailureReason.DNS_FAILURE.value: "DNS resolution failed",
    FailureReason.CONNECTION_REFUSED.value: "Connection refused",
    FailureReason.CONNECTION_TIMEOUT.value: "Connection timeout",
    FailureReason.READ_TIMEOUT.value: "Read timeout",
    FailureReason.TLS_ERROR.value: "TLS error",
    FailureReason.CERT_EXPIRED.value: "Certificate expired",
    FailureReason.CERT_INVALID.value: "Certificate invalid",
    FailureReason.HTTP_STATUS_MISMATCH.value: "Unexpected HTTP status",
    FailureReason.TOO_MANY_REDIRECTS.value: "Too many redirects",
    FailureReason.SLOW_RESPONSE.value: "Slow response",
    FailureReason.BLOCKED_TARGET.value: "Target not permitted",
    FailureReason.CONFIG_ERROR.value: "Configuration error",
    FailureReason.UNKNOWN_ERROR.value: "Unknown error",
}


def humanise_reason(reason: str | None) -> str:
    if not reason or reason == FailureReason.NONE.value:
        return "Unknown"
    return _HUMAN_REASONS.get(reason, reason.replace("_", " ").capitalize())


@dataclass
class RecordedCheck:
    """What happened as a consequence of one check."""

    result: MonitoringResult
    previous_status: str
    new_status: str
    incident_opened: Incident | None = None
    incident_closed: Incident | None = None
    certificate: SslCertificate | None = None
    alerts_raised: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.alerts_raised is None:
            self.alerts_raised = []

    @property
    def status_changed(self) -> bool:
        return self.previous_status != self.new_status


# ------------------------------------------------------------- thresholds
def resolve_thresholds(endpoint: Endpoint, config: dict[str, Any]) -> dict[str, int]:
    """Per-endpoint overrides win; otherwise the runtime settings apply."""
    return {
        "failure_threshold": int(
            endpoint.failure_threshold or config.get("failure_threshold", 3)
        ),
        "recovery_threshold": int(config.get("recovery_threshold", 1)),
        "ssl_warning_days": int(
            endpoint.ssl_warning_days or config.get("ssl_warning_days", 30)
        ),
        "ssl_critical_days": int(
            endpoint.ssl_critical_days or config.get("ssl_critical_days", 7)
        ),
        "response_time_threshold_ms": int(
            endpoint.response_time_threshold_ms
            or config.get("response_time_threshold_ms", 2000)
        ),
    }


# ---------------------------------------------------------- run a check
async def execute_check(
    endpoint: Endpoint, config: dict[str, Any]
) -> CheckOutcome:
    """Probe an endpoint without touching the database."""
    thresholds = resolve_thresholds(endpoint, config)
    secret: str | None = None
    if endpoint.auth_secret_encrypted:
        secret = decrypt_secret(endpoint.auth_secret_encrypted)
        if secret is None:
            logger.error(
                "endpoint_credential_undecryptable",
                endpoint=endpoint.name,
                endpoint_id=str(endpoint.id),
            )
            outcome = CheckOutcome()
            outcome.status = CheckStatus.DOWN.value
            outcome.failure_reason = FailureReason.CONFIG_ERROR.value
            outcome.error_message = (
                "Stored credentials could not be decrypted; re-enter the "
                "endpoint authentication settings"
            )
            return outcome

    target = build_target_from_endpoint(
        endpoint,
        auth_secret=secret,
        defaults={
            "ssl_warning_days": thresholds["ssl_warning_days"],
            "ssl_critical_days": thresholds["ssl_critical_days"],
            "response_time_threshold_ms": thresholds["response_time_threshold_ms"],
        },
    )
    return await run_check(target)


# ------------------------------------------------------- certificate rows
async def _persist_certificate(
    session: AsyncSession,
    endpoint: Endpoint,
    info: CertificateInfo,
    *,
    checked_at: datetime,
) -> SslCertificate | None:
    """Upsert the certificate observation, keeping a row per distinct cert."""
    if info is None:
        return None

    if not info.fingerprint_sha256:
        # Nothing to identify a certificate by - record the failure on the
        # endpoint so the SSL page can show "Unable to Check" with a reason.
        endpoint.ssl_status = info.status or SslStatus.UNABLE_TO_CHECK.value
        return None

    current = (
        await session.execute(
            select(SslCertificate)
            .where(
                SslCertificate.endpoint_id == endpoint.id,
                SslCertificate.is_current.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    payload = info.to_dict()

    if current is not None and current.fingerprint_sha256 == info.fingerprint_sha256:
        # Same certificate: refresh the volatile fields only.
        for field in (
            "days_remaining",
            "status",
            "tls_version",
            "tls_cipher",
            "hostname_matches",
            "chain_verified",
            "verification_status",
            "verification_error",
            "chain",
            "chain_length",
        ):
            setattr(current, field, payload.get(field))
        current.checked_at = checked_at
        record = current
    else:
        if current is not None:
            current.is_current = False
            logger.info(
                "certificate_rotated",
                endpoint=endpoint.name,
                old_fingerprint=current.fingerprint_sha256,
                new_fingerprint=info.fingerprint_sha256,
            )
        record = SslCertificate(
            endpoint_id=endpoint.id,
            is_current=True,
            first_seen_at=checked_at,
            checked_at=checked_at,
            **payload,
        )
        session.add(record)

    endpoint.ssl_status = info.status
    endpoint.ssl_expires_at = info.valid_to
    endpoint.ssl_days_remaining = info.days_remaining
    endpoint.ssl_issuer = (info.issuer_common_name or info.issuer or "")[:255] or None
    endpoint.ssl_common_name = (info.common_name or "")[:255] or None

    await session.flush()
    return record


# ------------------------------------------------------------- incidents
def _timeline_entry(kind: str, detail: str, at: datetime) -> dict[str, Any]:
    return {"at": at.isoformat(), "kind": kind, "detail": detail[:500]}


async def _open_incident(
    session: AsyncSession,
    endpoint: Endpoint,
    outcome: CheckOutcome,
    *,
    failed_checks: int,
) -> Incident | None:
    """Open an incident, tolerating a concurrent worker doing the same.

    The partial unique index on ``incidents`` guarantees a single open incident
    per endpoint; on conflict we adopt the row the other worker created instead
    of failing the check.
    """
    incident = Incident(
        endpoint_id=endpoint.id,
        status=IncidentStatus.OPEN.value,
        severity=Severity.CRITICAL.value,
        started_at=outcome.checked_at,
        reason=outcome.failure_reason,
        error_message=outcome.error_message,
        first_failure_status_code=outcome.http_status_code,
        failed_check_count=failed_checks,
        timeline=[
            _timeline_entry(
                "opened",
                f"{humanise_reason(outcome.failure_reason)}: "
                f"{outcome.error_message or 'no further detail'}",
                outcome.checked_at,
            )
        ],
    )
    # A SAVEPOINT rather than a plain flush: if the unique index rejects this
    # insert, only the incident is rolled back - the monitoring result written
    # earlier in the same transaction survives.
    try:
        async with session.begin_nested():
            session.add(incident)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(Incident).where(
                    Incident.endpoint_id == endpoint.id,
                    Incident.status == IncidentStatus.OPEN.value,
                )
            )
        ).scalar_one_or_none()
        logger.info(
            "incident_open_race_resolved",
            endpoint=endpoint.name,
            incident_id=existing.id if existing else None,
        )
        return existing
    logger.info(
        "incident_opened",
        endpoint=endpoint.name,
        incident_id=incident.id,
        reason=outcome.failure_reason,
    )
    return incident


async def _get_open_incident(
    session: AsyncSession, endpoint_id: uuid.UUID
) -> Incident | None:
    return (
        await session.execute(
            select(Incident)
            .where(
                Incident.endpoint_id == endpoint_id,
                Incident.status == IncidentStatus.OPEN.value,
            )
            .order_by(Incident.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _close_incident(
    incident: Incident, outcome: CheckOutcome
) -> Incident:
    incident.status = IncidentStatus.RESOLVED.value
    incident.resolved_at = outcome.checked_at
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    incident.duration_seconds = max(
        0, int((outcome.checked_at - started).total_seconds())
    )
    incident.recovery_status_code = outcome.http_status_code
    incident.recovery_response_time_ms = outcome.response_time_ms
    timeline = list(incident.timeline or [])
    timeline.append(
        _timeline_entry(
            "resolved",
            f"Recovered with HTTP {outcome.http_status_code or '-'} in "
            f"{outcome.response_time_ms:.0f} ms"
            if outcome.response_time_ms is not None
            else "Recovered",
            outcome.checked_at,
        )
    )
    incident.timeline = timeline
    return incident


# ---------------------------------------------------------- main entry point
async def record_check_result(
    session: AsyncSession,
    endpoint: Endpoint,
    outcome: CheckOutcome,
    *,
    config: dict[str, Any],
    checked_by: str | None = None,
    is_manual: bool = False,
    dispatch_notifications: bool = True,
) -> RecordedCheck:
    """Persist a check and apply every downstream state transition."""
    thresholds = resolve_thresholds(endpoint, config)
    previous_status = endpoint.current_status
    now = outcome.checked_at

    result = MonitoringResult(
        endpoint_id=endpoint.id,
        checked_at=now,
        status=outcome.status,
        http_status_code=outcome.http_status_code,
        response_time_ms=outcome.response_time_ms,
        dns_time_ms=outcome.dns_time_ms,
        connect_time_ms=outcome.connect_time_ms,
        tls_time_ms=outcome.tls_time_ms,
        ttfb_ms=outcome.ttfb_ms,
        total_time_ms=outcome.total_time_ms,
        resolved_ip=outcome.resolved_ip,
        content_length=outcome.content_length,
        redirect_count=outcome.redirect_count,
        final_url=outcome.final_url,
        redirect_chain=outcome.redirect_chain or None,
        response_headers=outcome.response_headers or None,
        error_message=outcome.error_message,
        failure_reason=outcome.failure_reason,
        tls_version=outcome.tls_version,
        tls_cipher=outcome.tls_cipher,
        cert_expires_at=outcome.cert_expires_at,
        ssl_days_remaining=outcome.ssl_days_remaining,
        ssl_status=outcome.ssl_status,
        checked_by=checked_by,
        is_manual=is_manual,
    )
    session.add(result)

    # ------------------------------------------------------ live state
    endpoint.last_checked_at = now
    endpoint.last_status_code = outcome.http_status_code
    endpoint.last_response_time_ms = outcome.response_time_ms
    endpoint.last_error = outcome.error_message
    endpoint.total_checks = (endpoint.total_checks or 0) + 1

    recorded = RecordedCheck(
        result=result,
        previous_status=previous_status,
        new_status=previous_status,
    )

    # -------------------------------------------------- certificate rows
    if outcome.certificate is not None and endpoint.ssl_monitoring_enabled:
        recorded.certificate = await _persist_certificate(
            session, endpoint, outcome.certificate, checked_at=now
        )
    elif endpoint.protocol != "https" or not endpoint.ssl_monitoring_enabled:
        endpoint.ssl_status = SslStatus.NOT_APPLICABLE.value

    open_incident = await _get_open_incident(session, endpoint.id)

    if outcome.is_up:
        endpoint.consecutive_successes = (endpoint.consecutive_successes or 0) + 1
        endpoint.consecutive_failures = 0
        endpoint.current_status = (
            EndpointStatus.DEGRADED.value
            if outcome.status == CheckStatus.DEGRADED.value
            else EndpointStatus.UP.value
        )

        if open_incident is not None and (
            endpoint.consecutive_successes >= thresholds["recovery_threshold"]
        ):
            _close_incident(open_incident, outcome)
            recorded.incident_closed = open_incident
            await session.flush()
            alert = await alert_service.raise_alert(
                session,
                alert_type=AlertType.ENDPOINT_RECOVERED.value,
                endpoint=endpoint,
                incident_id=open_incident.id,
                title=f"Endpoint recovered: {endpoint.name}",
                message=(
                    f"{endpoint.url} is responding again after "
                    f"{_format_duration(open_incident.duration_seconds)} of downtime."
                ),
                severity=Severity.INFO.value,
                details={
                    "http_status_code": outcome.http_status_code,
                    "response_time_ms": outcome.response_time_ms,
                    "downtime_seconds": open_incident.duration_seconds,
                    "failed_checks": open_incident.failed_check_count,
                    "incident_id": open_incident.id,
                },
                config=config,
                dispatch=dispatch_notifications,
            )
            if alert:
                recorded.alerts_raised.append(alert)
            logger.info(
                "incident_resolved",
                endpoint=endpoint.name,
                incident_id=open_incident.id,
                duration_seconds=open_incident.duration_seconds,
            )

        if outcome.status == CheckStatus.DEGRADED.value and config.get(
            "alert_on_degraded", True
        ):
            alert = await alert_service.raise_alert(
                session,
                alert_type=AlertType.HIGH_RESPONSE_TIME.value,
                endpoint=endpoint,
                title=f"High response time: {endpoint.name}",
                message=outcome.error_message
                or f"{endpoint.url} responded slowly.",
                severity=Severity.WARNING.value,
                details={
                    "response_time_ms": outcome.response_time_ms,
                    "threshold_ms": thresholds["response_time_threshold_ms"],
                    "http_status_code": outcome.http_status_code,
                },
                config=config,
                dispatch=dispatch_notifications,
            )
            if alert:
                recorded.alerts_raised.append(alert)
    else:
        endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
        endpoint.consecutive_successes = 0
        endpoint.total_failures = (endpoint.total_failures or 0) + 1
        endpoint.current_status = EndpointStatus.DOWN.value

        failures = endpoint.consecutive_failures
        threshold = thresholds["failure_threshold"]

        if open_incident is not None:
            # Already in an incident: extend it rather than opening another.
            open_incident.failed_check_count += 1
            if open_incident.reason != outcome.failure_reason:
                timeline = list(open_incident.timeline or [])
                timeline.append(
                    _timeline_entry(
                        "reason_changed",
                        f"{humanise_reason(outcome.failure_reason)}: "
                        f"{outcome.error_message or 'no further detail'}",
                        now,
                    )
                )
                open_incident.timeline = timeline[-50:]
                open_incident.reason = outcome.failure_reason
                open_incident.error_message = outcome.error_message
        elif failures >= threshold:
            incident = await _open_incident(
                session, endpoint, outcome, failed_checks=failures
            )
            recorded.incident_opened = incident
            alert = await alert_service.raise_alert(
                session,
                alert_type=AlertType.ENDPOINT_DOWN.value,
                endpoint=endpoint,
                incident_id=incident.id if incident else None,
                title=f"Endpoint DOWN: {endpoint.name}",
                message=(
                    f"{endpoint.url} failed {failures} consecutive check(s). "
                    f"{humanise_reason(outcome.failure_reason)}"
                    f"{': ' + outcome.error_message if outcome.error_message else ''}"
                ),
                severity=Severity.CRITICAL.value,
                details={
                    "failure_reason": outcome.failure_reason,
                    "error": outcome.error_message,
                    "http_status_code": outcome.http_status_code,
                    "consecutive_failures": failures,
                    "incident_id": incident.id if incident else None,
                },
                config=config,
                dispatch=dispatch_notifications,
            )
            if alert:
                recorded.alerts_raised.append(alert)

        # A sustained outage gets one escalation notice, well past the point
        # where the first alert could have been missed.
        if failures == threshold * 4:
            alert = await alert_service.raise_alert(
                session,
                alert_type=AlertType.REPEATED_FAILURES.value,
                endpoint=endpoint,
                incident_id=open_incident.id if open_incident else None,
                title=f"Repeated failures: {endpoint.name}",
                message=(
                    f"{endpoint.url} has now failed {failures} consecutive checks "
                    "without recovering."
                ),
                severity=Severity.CRITICAL.value,
                details={
                    "consecutive_failures": failures,
                    "failure_reason": outcome.failure_reason,
                    "error": outcome.error_message,
                },
                config=config,
                dispatch=dispatch_notifications,
            )
            if alert:
                recorded.alerts_raised.append(alert)

    recorded.new_status = endpoint.current_status

    # ------------------------------------------------------- SSL alerts
    if outcome.certificate is not None and endpoint.ssl_monitoring_enabled:
        ssl_alert = await alert_service.evaluate_ssl_alert(
            session,
            endpoint,
            outcome.certificate,
            config=config,
            dispatch=dispatch_notifications,
        )
        if ssl_alert:
            recorded.alerts_raised.append(ssl_alert)

    await session.flush()

    logger.info(
        "check_recorded",
        endpoint=endpoint.name,
        endpoint_id=str(endpoint.id),
        status=outcome.status,
        previous_status=previous_status,
        http_status=outcome.http_status_code,
        response_time_ms=outcome.response_time_ms,
        failure_reason=outcome.failure_reason
        if outcome.failure_reason != FailureReason.NONE.value
        else None,
        incident_opened=bool(recorded.incident_opened),
        incident_closed=bool(recorded.incident_closed),
        manual=is_manual,
    )
    return recorded


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "less than a minute"
    if seconds < 60:
        return f"{seconds} second(s)"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minute(s)" + (f" {secs}s" if secs else "")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


# ------------------------------------------------- certificate re-grading
async def regrade_certificates(
    session: AsyncSession, *, warning_days: int, critical_days: int
) -> int:
    """Recompute certificate states after a threshold change.

    Without this, editing the SSL warning threshold would only take effect as
    each endpoint happened to be checked again.
    """
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(SslCertificate).where(SslCertificate.is_current.is_(True))
        )
    ).scalars().all()

    updated = 0
    for row in rows:
        if row.valid_to is None:
            continue
        valid_to = row.valid_to
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=timezone.utc)
        days = int((valid_to - now).total_seconds() // 86400)
        structurally_valid = row.chain_verified is not False and (
            row.hostname_matches is not False
        )
        status = classify_certificate(
            days,
            warning_days=warning_days,
            critical_days=critical_days,
            is_valid=structurally_valid,
            verification_failed=row.chain_verified is False,
        )
        if row.days_remaining != days or row.status != status:
            row.days_remaining = days
            row.status = status
            updated += 1
            await session.execute(
                update(Endpoint)
                .where(Endpoint.id == row.endpoint_id)
                .values(
                    ssl_status=status,
                    ssl_days_remaining=days,
                    ssl_expires_at=row.valid_to,
                )
            )
    if updated:
        await session.flush()
        logger.info("certificates_regraded", updated=updated)
    return updated


def next_check_time(interval_seconds: int, *, jitter_ratio: float = 0.1) -> datetime:
    """Schedule the next run with a little jitter.

    Without jitter, endpoints created by a bulk import all share a due time
    forever and arrive as a thundering herd every interval.
    """
    import random

    jitter = interval_seconds * jitter_ratio
    offset = interval_seconds + random.uniform(-jitter, jitter)
    return datetime.now(timezone.utc) + timedelta(seconds=max(5.0, offset))
