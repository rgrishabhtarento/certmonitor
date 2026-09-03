"""Alert delivery.

A generic webhook is the baseline mechanism required of the system; Slack,
Microsoft Teams, PagerDuty and SMTP e-mail are implemented on top of the same
channel abstraction, so adding another provider means adding one function to
``_DELIVERY`` rather than touching the alerting logic.

Channel configuration (webhook URLs, SMTP passwords, routing keys) is stored as
a single encrypted blob and is never returned to a client. Only
``config_public`` - host names, ports, recipient counts - is displayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import NotificationChannelType, SEVERITY_ORDER, Severity
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.alert import Alert, NotificationChannel

logger = get_logger(__name__)

DELIVERY_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 3

_SEVERITY_COLOURS = {
    Severity.INFO.value: "#2563eb",
    Severity.WARNING.value: "#d97706",
    Severity.CRITICAL.value: "#dc2626",
}

_SEVERITY_EMOJI = {
    Severity.INFO.value: ":large_blue_circle:",
    Severity.WARNING.value: ":large_yellow_circle:",
    Severity.CRITICAL.value: ":red_circle:",
}


class NotificationError(RuntimeError):
    """Delivery failed; the alert row records the reason."""


# ------------------------------------------------------------ config crypto
_SECRET_KEYS_BY_TYPE: dict[str, set[str]] = {
    NotificationChannelType.WEBHOOK.value: {"url", "secret", "headers"},
    NotificationChannelType.SLACK.value: {"webhook_url"},
    NotificationChannelType.TEAMS.value: {"webhook_url"},
    NotificationChannelType.PAGERDUTY.value: {"routing_key"},
    NotificationChannelType.EMAIL.value: {"password"},
}

REQUIRED_CONFIG: dict[str, tuple[str, ...]] = {
    NotificationChannelType.WEBHOOK.value: ("url",),
    NotificationChannelType.SLACK.value: ("webhook_url",),
    NotificationChannelType.TEAMS.value: ("webhook_url",),
    NotificationChannelType.PAGERDUTY.value: ("routing_key",),
    NotificationChannelType.EMAIL.value: ("host", "from_address", "recipients"),
}


def validate_config(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Check required keys and normalise a channel configuration."""
    if channel_type not in REQUIRED_CONFIG:
        raise ValueError(f"unsupported channel type '{channel_type}'")

    cleaned = dict(config or {})
    missing = [key for key in REQUIRED_CONFIG[channel_type] if not cleaned.get(key)]
    if missing:
        raise ValueError(
            f"{channel_type} channel requires: " + ", ".join(missing)
        )

    for url_key in ("url", "webhook_url"):
        if cleaned.get(url_key):
            parsed = urlsplit(str(cleaned[url_key]))
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"{url_key} must be an absolute http(s) URL")

    if channel_type == NotificationChannelType.WEBHOOK.value:
        method = str(cleaned.get("method", "POST")).upper()
        if method not in ("POST", "PUT", "PATCH"):
            raise ValueError("webhook method must be POST, PUT or PATCH")
        cleaned["method"] = method
        headers = cleaned.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("webhook headers must be an object")
        cleaned["headers"] = {str(k): str(v) for k, v in headers.items()}

    if channel_type == NotificationChannelType.EMAIL.value:
        recipients = cleaned.get("recipients")
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("email channel requires at least one recipient")
        cleaned["recipients"] = recipients
        cleaned["port"] = int(cleaned.get("port") or 587)
        cleaned["use_tls"] = bool(cleaned.get("use_tls", True))
        cleaned["use_ssl"] = bool(cleaned.get("use_ssl", False))

    return cleaned


def public_view(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """The subset of a config that is safe to show in the UI."""
    public: dict[str, Any] = {}
    if channel_type in (
        NotificationChannelType.WEBHOOK.value,
        NotificationChannelType.SLACK.value,
        NotificationChannelType.TEAMS.value,
    ):
        raw = config.get("url") or config.get("webhook_url") or ""
        parsed = urlsplit(str(raw))
        public["target_host"] = parsed.netloc or None
        public["target_scheme"] = parsed.scheme or None
        if channel_type == NotificationChannelType.WEBHOOK.value:
            public["method"] = config.get("method", "POST")
            public["custom_header_names"] = sorted((config.get("headers") or {}).keys())
            public["signed"] = bool(config.get("secret"))
    elif channel_type == NotificationChannelType.PAGERDUTY.value:
        public["routing_key_configured"] = bool(config.get("routing_key"))
    elif channel_type == NotificationChannelType.EMAIL.value:
        public.update(
            {
                "host": config.get("host"),
                "port": config.get("port"),
                "use_tls": config.get("use_tls"),
                "use_ssl": config.get("use_ssl"),
                "from_address": config.get("from_address"),
                "recipient_count": len(config.get("recipients") or []),
                "authenticated": bool(config.get("username")),
            }
        )
    return public


def encrypt_config(config: dict[str, Any]) -> str:
    return encrypt_secret(json.dumps(config))


def decrypt_config(blob: str | None) -> dict[str, Any] | None:
    if not blob:
        return None
    raw = decrypt_secret(blob)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------- payloads
def build_payload(alert: Alert) -> dict[str, Any]:
    """Canonical JSON body used by the generic webhook channel."""
    endpoint = alert.endpoint
    return {
        "event": alert.alert_type,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message,
        "occurred_at": (alert.created_at or datetime.now(timezone.utc)).isoformat(),
        "alert_id": alert.id,
        "incident_id": alert.incident_id,
        "endpoint": (
            {
                "id": str(endpoint.id),
                "name": endpoint.name,
                "url": endpoint.url,
                "hostname": endpoint.hostname,
                "environment": endpoint.environment.name if endpoint.environment else None,
                "tags": endpoint.tag_names,
                "owner": endpoint.owner,
                "team": endpoint.team,
                "current_status": endpoint.current_status,
            }
            if endpoint
            else None
        ),
        "details": alert.details or {},
        "source": "certmonitor",
    }


def _summary_fields(payload: dict[str, Any]) -> list[tuple[str, str]]:
    endpoint = payload.get("endpoint") or {}
    details = payload.get("details") or {}
    fields: list[tuple[str, str]] = []
    if endpoint.get("url"):
        fields.append(("Endpoint", f"{endpoint.get('name')} ({endpoint['url']})"))
    if endpoint.get("environment"):
        fields.append(("Environment", str(endpoint["environment"])))
    for key, label in (
        ("http_status_code", "HTTP status"),
        ("response_time_ms", "Response time (ms)"),
        ("failure_reason", "Failure reason"),
        ("consecutive_failures", "Consecutive failures"),
        ("days_remaining", "Days remaining"),
        ("expires_at", "Expires"),
        ("issuer", "Issuer"),
        ("downtime_seconds", "Downtime (s)"),
    ):
        if details.get(key) is not None:
            fields.append((label, str(details[key])))
    if endpoint.get("owner"):
        fields.append(("Owner", str(endpoint["owner"])))
    return fields


# --------------------------------------------------------------- delivery
async def _deliver_webhook(config: dict[str, Any], payload: dict[str, Any]) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "CertMonitor/1.0"}
    headers.update(config.get("headers") or {})

    secret = config.get("secret")
    if secret:
        # HMAC-SHA256 over the exact bytes sent, so the receiver can verify
        # the payload actually came from this instance.
        signature = hmac.new(
            str(secret).encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        headers["X-CertMonitor-Signature"] = f"sha256={signature}"

    async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS, trust_env=False) as client:
        response = await client.request(
            config.get("method", "POST"),
            config["url"],
            content=body,
            headers=headers,
        )
    if response.status_code >= 400:
        raise NotificationError(
            f"webhook responded {response.status_code}: {response.text[:200]}"
        )


async def _deliver_slack(config: dict[str, Any], payload: dict[str, Any]) -> None:
    emoji = _SEVERITY_EMOJI.get(payload["severity"], "")
    lines = [f"{emoji} *{payload['title']}*"]
    if payload.get("message"):
        lines.append(payload["message"])
    for label, value in _summary_fields(payload):
        lines.append(f"• *{label}:* {value}")

    body = {
        "text": f"{payload['title']}",
        "attachments": [
            {
                "color": _SEVERITY_COLOURS.get(payload["severity"], "#6b7280"),
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                    }
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS, trust_env=False) as client:
        response = await client.post(config["webhook_url"], json=body)
    if response.status_code >= 400:
        raise NotificationError(
            f"Slack responded {response.status_code}: {response.text[:200]}"
        )


async def _deliver_teams(config: dict[str, Any], payload: dict[str, Any]) -> None:
    body = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": _SEVERITY_COLOURS.get(payload["severity"], "6b7280").lstrip("#"),
        "summary": payload["title"],
        "title": payload["title"],
        "text": payload.get("message") or "",
        "sections": [
            {
                "facts": [
                    {"name": label, "value": value}
                    for label, value in _summary_fields(payload)
                ]
            }
        ],
    }
    async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS, trust_env=False) as client:
        response = await client.post(config["webhook_url"], json=body)
    if response.status_code >= 400:
        raise NotificationError(
            f"Teams responded {response.status_code}: {response.text[:200]}"
        )


_PAGERDUTY_SEVERITY = {
    Severity.INFO.value: "info",
    Severity.WARNING.value: "warning",
    Severity.CRITICAL.value: "critical",
}


async def _deliver_pagerduty(config: dict[str, Any], payload: dict[str, Any]) -> None:
    endpoint = payload.get("endpoint") or {}
    # Recovery events resolve the incident PagerDuty already has open, keyed by
    # endpoint id, rather than creating a new one.
    is_recovery = payload["event"] == "endpoint_recovered"
    dedup_key = f"certmonitor:{endpoint.get('id') or payload['alert_id']}"
    body = {
        "routing_key": config["routing_key"],
        "event_action": "resolve" if is_recovery else "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary": payload["title"][:1024],
            "severity": _PAGERDUTY_SEVERITY.get(payload["severity"], "warning"),
            "source": endpoint.get("hostname") or "certmonitor",
            "component": endpoint.get("name"),
            "group": endpoint.get("environment"),
            "class": payload["event"],
            "custom_details": payload.get("details") or {},
        },
    }
    async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS, trust_env=False) as client:
        response = await client.post(
            "https://events.pagerduty.com/v2/enqueue", json=body
        )
    if response.status_code >= 400:
        raise NotificationError(
            f"PagerDuty responded {response.status_code}: {response.text[:200]}"
        )


def _send_email_blocking(config: dict[str, Any], payload: dict[str, Any]) -> None:
    message = EmailMessage()
    message["Subject"] = f"[{payload['severity'].upper()}] {payload['title']}"
    message["From"] = config["from_address"]
    message["To"] = ", ".join(config["recipients"])

    lines = [payload["title"], ""]
    if payload.get("message"):
        lines.extend([payload["message"], ""])
    for label, value in _summary_fields(payload):
        lines.append(f"{label}: {value}")
    lines.extend(["", f"Occurred at: {payload['occurred_at']}", "", "-- CertMonitor"])
    message.set_content("\n".join(lines))

    host = config["host"]
    port = int(config.get("port") or 587)
    timeout = DELIVERY_TIMEOUT_SECONDS

    if config.get("use_ssl"):
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        if config.get("use_tls") and not config.get("use_ssl"):
            server.starttls()
            server.ehlo()
        if config.get("username"):
            server.login(config["username"], config.get("password") or "")
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # pragma: no cover
            pass


async def _deliver_email(config: dict[str, Any], payload: dict[str, Any]) -> None:
    # smtplib is blocking; run it off the event loop so a slow mail server
    # cannot stall the worker.
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_send_email_blocking, config, payload),
            timeout=DELIVERY_TIMEOUT_SECONDS * 2,
        )
    except asyncio.TimeoutError as exc:
        raise NotificationError("SMTP delivery timed out") from exc
    except smtplib.SMTPException as exc:
        raise NotificationError(f"SMTP error: {exc}") from exc
    except OSError as exc:
        raise NotificationError(f"SMTP connection failed: {exc}") from exc


_DELIVERY: dict[str, Callable[[dict[str, Any], dict[str, Any]], Coroutine[Any, Any, None]]] = {
    NotificationChannelType.WEBHOOK.value: _deliver_webhook,
    NotificationChannelType.SLACK.value: _deliver_slack,
    NotificationChannelType.TEAMS.value: _deliver_teams,
    NotificationChannelType.PAGERDUTY.value: _deliver_pagerduty,
    NotificationChannelType.EMAIL.value: _deliver_email,
}


# ------------------------------------------------------------- orchestration
def channel_matches(channel: NotificationChannel, alert: Alert) -> bool:
    """Apply a channel's severity, event, environment and tag filters."""
    if not channel.is_enabled:
        return False
    if SEVERITY_ORDER.get(alert.severity, 1) < SEVERITY_ORDER.get(
        channel.min_severity, 1
    ):
        return False
    if channel.event_types and alert.alert_type not in channel.event_types:
        return False

    endpoint = alert.endpoint
    if channel.environment_filter:
        env_name = endpoint.environment.name if endpoint and endpoint.environment else None
        if env_name not in channel.environment_filter:
            return False
    if channel.tag_filter:
        endpoint_tags = set(endpoint.tag_names) if endpoint else set()
        if not endpoint_tags & set(channel.tag_filter):
            return False
    return True


async def deliver_to_channel(
    channel: NotificationChannel, alert: Alert, payload: dict[str, Any]
) -> None:
    config = decrypt_config(channel.config_encrypted)
    if config is None:
        raise NotificationError(
            "channel configuration could not be decrypted - it must be re-entered "
            "after an encryption key change"
        )
    handler = _DELIVERY.get(channel.channel_type)
    if handler is None:
        raise NotificationError(f"no handler for channel type '{channel.channel_type}'")

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await handler(config, payload)
            return
        except (NotificationError, httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                # Exponential backoff: a transient 502 from a webhook receiver
                # should not lose the alert.
                await asyncio.sleep(min(2 ** attempt, 8))
    raise NotificationError(str(last_error) if last_error else "delivery failed")


async def dispatch_alert(session: AsyncSession, alert: Alert) -> dict[str, Any]:
    """Send one alert to every channel that matches it.

    Updates the alert's notification bookkeeping. Returns a per-channel result
    summary; never raises, because a failed notification must not roll back the
    monitoring result that produced it.
    """
    channels = (
        await session.execute(
            select(NotificationChannel).where(NotificationChannel.is_enabled.is_(True))
        )
    ).scalars().all()

    matching = [ch for ch in channels if channel_matches(ch, alert)]
    if not matching:
        alert.notification_status = "skipped"
        return {"delivered": 0, "failed": 0, "channels": []}

    payload = build_payload(alert)
    results: list[dict[str, Any]] = []
    delivered = failed = 0

    for channel in matching:
        alert.notification_attempts += 1
        try:
            await deliver_to_channel(channel, alert, payload)
            channel.success_count += 1
            channel.last_used_at = datetime.now(timezone.utc)
            channel.last_error = None
            delivered += 1
            results.append({"channel": channel.name, "status": "delivered"})
        except Exception as exc:
            channel.failure_count += 1
            channel.last_error = str(exc)[:1000]
            failed += 1
            results.append(
                {"channel": channel.name, "status": "failed", "error": str(exc)[:300]}
            )
            logger.warning(
                "notification_failed",
                channel=channel.name,
                channel_type=channel.channel_type,
                alert_type=alert.alert_type,
                error=str(exc),
            )

    if delivered and not failed:
        alert.notification_status = "sent"
    elif delivered:
        alert.notification_status = "partial"
    else:
        alert.notification_status = "failed"
    alert.notification_error = next(
        (r.get("error") for r in results if r["status"] == "failed"), None
    )
    if delivered:
        alert.notified_at = datetime.now(timezone.utc)

    return {"delivered": delivered, "failed": failed, "channels": results}


async def send_test_notification(channel: NotificationChannel) -> None:
    """Deliver a synthetic payload so an operator can verify a channel."""
    payload = {
        "event": "test",
        "severity": Severity.INFO.value,
        "title": f"CertMonitor test notification ({channel.name})",
        "message": "If you can read this, the channel is configured correctly.",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "alert_id": 0,
        "incident_id": None,
        "endpoint": None,
        "details": {"channel_type": channel.channel_type},
        "source": "certmonitor",
    }
    config = decrypt_config(channel.config_encrypted)
    if config is None:
        raise NotificationError("channel configuration could not be decrypted")
    handler = _DELIVERY.get(channel.channel_type)
    if handler is None:
        raise NotificationError(f"no handler for channel type '{channel.channel_type}'")
    await handler(config, payload)
