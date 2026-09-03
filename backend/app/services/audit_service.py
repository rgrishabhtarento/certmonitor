"""Audit trail writer.

Every administrative mutation funnels through :func:`record` so the trail is
consistent and impossible to forget in a route handler. Details are scrubbed of
anything credential-shaped before they are stored.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.system import AuditLog
from app.models.user import User

logger = get_logger(__name__)

_SENSITIVE_FIELDS = {
    "password",
    "new_password",
    "current_password",
    "old_password",
    "auth_secret",
    "auth_credentials",
    "token",
    "secret",
    "api_key",
    "config",
    "webhook_url",
    "smtp_password",
    "routing_key",
}


def scrub(payload: Any, *, depth: int = 0) -> Any:
    """Recursively replace credential-shaped values with a marker."""
    if depth > 4:
        return "..."
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in _SENSITIVE_FIELDS:
                cleaned[key] = "***redacted***"
            else:
                cleaned[key] = scrub(value, depth=depth + 1)
        return cleaned
    if isinstance(payload, (list, tuple)):
        return [scrub(item, depth=depth + 1) for item in payload][:50]
    if isinstance(payload, uuid.UUID):
        return str(payload)
    if isinstance(payload, str) and len(payload) > 512:
        return payload[:512] + "..."
    return payload


def client_ip(request: Request | None) -> str | None:
    """Best-effort client address.

    ``X-Forwarded-For`` is honoured because the app normally sits behind nginx
    or an ingress; the left-most entry is taken.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64]
    if request.client:
        return request.client.host
    return None


async def record(
    session: AsyncSession,
    *,
    action: str,
    user: User | None = None,
    username: str | None = None,
    resource_type: str | None = None,
    resource_id: Any | None = None,
    resource_name: str | None = None,
    details: dict[str, Any] | None = None,
    status: str = "success",
    request: Request | None = None,
) -> AuditLog:
    """Append an audit entry. Never raises into the caller's flow."""
    entry = AuditLog(
        user_id=user.id if user else None,
        username=(user.username if user else username),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        resource_name=(resource_name[:255] if resource_name else None),
        details=scrub(details) if details else None,
        status=status,
        ip_address=client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
        request_method=(request.method if request else None),
        request_path=(str(request.url.path)[:512] if request else None),
    )
    session.add(entry)
    try:
        await session.flush()
    except Exception as exc:  # pragma: no cover - audit must not break the API
        logger.error("audit_write_failed", action=action, error=str(exc))
    logger.info(
        "audit",
        action=action,
        actor=entry.username,
        resource_type=resource_type,
        resource_id=entry.resource_id,
        status=status,
        ip=entry.ip_address,
    )
    return entry


def diff_fields(
    before: dict[str, Any], after: dict[str, Any], *, keys: list[str] | None = None
) -> dict[str, Any]:
    """Produce a compact ``{field: {from, to}}`` map for audit details."""
    changed: dict[str, Any] = {}
    candidate_keys = keys or sorted(set(before) | set(after))
    for key in candidate_keys:
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changed[key] = {"from": scrub({key: old})[key], "to": scrub({key: new})[key]}
    return changed
