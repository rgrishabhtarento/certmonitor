"""Alert generation with cooldown suppression."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AlertType, Severity, SslStatus
from app.core.logging import get_logger
from app.models.alert import Alert
from app.models.endpoint import Endpoint
from app.services import notification_service

logger = get_logger(__name__)

# Recovery notices ignore the cooldown: suppressing an "all clear" is worse
# than sending one too many.
_COOLDOWN_EXEMPT = {AlertType.ENDPOINT_RECOVERED.value}

_DEFAULT_SEVERITY = {
    AlertType.ENDPOINT_DOWN.value: Severity.CRITICAL.value,
    AlertType.ENDPOINT_RECOVERED.value: Severity.INFO.value,
    AlertType.HIGH_RESPONSE_TIME.value: Severity.WARNING.value,
    AlertType.REPEATED_FAILURES.value: Severity.CRITICAL.value,
    AlertType.SSL_EXPIRING.value: Severity.WARNING.value,
    AlertType.SSL_EXPIRED.value: Severity.CRITICAL.value,
    AlertType.SSL_INVALID.value: Severity.CRITICAL.value,
}


async def in_cooldown(
    session: AsyncSession,
    *,
    endpoint_id: uuid.UUID | None,
    alert_type: str,
    cooldown_minutes: int,
) -> bool:
    """True when an equivalent alert was raised inside the cooldown window."""
    if cooldown_minutes <= 0 or alert_type in _COOLDOWN_EXEMPT:
        return False
    since = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    stmt = (
        select(Alert.id)
        .where(
            Alert.alert_type == alert_type,
            Alert.created_at >= since,
        )
        .limit(1)
    )
    if endpoint_id is None:
        stmt = stmt.where(Alert.endpoint_id.is_(None))
    else:
        stmt = stmt.where(Alert.endpoint_id == endpoint_id)
    return (await session.execute(stmt)).first() is not None


async def raise_alert(
    session: AsyncSession,
    *,
    alert_type: str,
    endpoint: Endpoint | None = None,
    incident_id: int | None = None,
    title: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    severity: str | None = None,
    config: dict[str, Any] | None = None,
    dispatch: bool = True,
) -> Alert | None:
    """Record an alert and (optionally) deliver it.

    Returns ``None`` when the alert was suppressed by the cooldown or by the
    global/per-endpoint alerting switches.
    """
    config = config or {}
    if not config.get("alerts_enabled", True):
        return None
    if endpoint is not None and not endpoint.alerts_enabled:
        return None

    cooldown = int(config.get("alert_cooldown_minutes", 30))
    if await in_cooldown(
        session,
        endpoint_id=endpoint.id if endpoint else None,
        alert_type=alert_type,
        cooldown_minutes=cooldown,
    ):
        logger.debug(
            "alert_suppressed_by_cooldown",
            alert_type=alert_type,
            endpoint=endpoint.name if endpoint else None,
        )
        return None

    alert = Alert(
        endpoint_id=endpoint.id if endpoint else None,
        incident_id=incident_id,
        alert_type=alert_type,
        severity=severity or _DEFAULT_SEVERITY.get(alert_type, Severity.WARNING.value),
        title=title[:255],
        message=message,
        details=details or {},
    )
    # Attach the loaded endpoint so payload building does not trigger a lazy
    # load on a detached instance.
    if endpoint is not None:
        alert.endpoint = endpoint
    session.add(alert)
    await session.flush()

    if dispatch and config.get("notifications_enabled", True):
        try:
            await notification_service.dispatch_alert(session, alert)
        except Exception as exc:  # pragma: no cover - defensive
            alert.notification_status = "failed"
            alert.notification_error = str(exc)[:1000]
            logger.error("alert_dispatch_error", alert_type=alert_type, error=str(exc))
    else:
        alert.notification_status = "skipped"

    logger.info(
        "alert_raised",
        alert_type=alert_type,
        severity=alert.severity,
        endpoint=endpoint.name if endpoint else None,
        notification_status=alert.notification_status,
    )
    return alert


# --------------------------------------------------------------- SSL alerts
def ssl_alert_for_status(status: str) -> str | None:
    if status == SslStatus.EXPIRED.value:
        return AlertType.SSL_EXPIRED.value
    if status == SslStatus.INVALID.value:
        return AlertType.SSL_INVALID.value
    if status in (SslStatus.CRITICAL.value, SslStatus.EXPIRING_SOON.value):
        return AlertType.SSL_EXPIRING.value
    return None


async def evaluate_ssl_alert(
    session: AsyncSession,
    endpoint: Endpoint,
    certificate_info: Any,
    *,
    config: dict[str, Any],
    dispatch: bool = True,
) -> Alert | None:
    """Raise the appropriate certificate alert, if any."""
    if certificate_info is None:
        return None
    status = certificate_info.status
    alert_type = ssl_alert_for_status(status)
    if alert_type is None:
        return None

    days = certificate_info.days_remaining
    expiry = certificate_info.valid_to
    issuer = certificate_info.issuer_common_name or certificate_info.issuer

    if alert_type == AlertType.SSL_EXPIRED.value:
        title = f"SSL certificate expired: {endpoint.name}"
        message = (
            f"The certificate for {endpoint.hostname} expired "
            f"{abs(days) if days is not None else '?'} day(s) ago."
        )
        severity = Severity.CRITICAL.value
    elif alert_type == AlertType.SSL_INVALID.value:
        title = f"SSL certificate invalid: {endpoint.name}"
        message = (
            certificate_info.verification_error
            or certificate_info.error
            or f"The certificate for {endpoint.hostname} failed validation."
        )
        severity = Severity.CRITICAL.value
    else:
        title = f"SSL certificate expiring in {days} day(s): {endpoint.name}"
        message = (
            f"The certificate for {endpoint.hostname} issued by {issuer} "
            f"expires on {expiry.date().isoformat() if expiry else 'unknown'}."
        )
        severity = (
            Severity.CRITICAL.value
            if status == SslStatus.CRITICAL.value
            else Severity.WARNING.value
        )

    return await raise_alert(
        session,
        alert_type=alert_type,
        endpoint=endpoint,
        title=title,
        message=message,
        severity=severity,
        details={
            "hostname": endpoint.hostname,
            "days_remaining": days,
            "expires_at": expiry.isoformat() if expiry else None,
            "issuer": issuer,
            "common_name": certificate_info.common_name,
            "ssl_status": status,
            "verification_status": certificate_info.verification_status,
        },
        config=config,
        dispatch=dispatch,
    )
