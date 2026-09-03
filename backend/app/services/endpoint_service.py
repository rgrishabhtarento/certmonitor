"""Endpoint creation, updates, tag/environment resolution and search."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import AuthType, CheckType, EndpointStatus, SslStatus
from app.core.logging import get_logger
from app.core.security import encrypt_secret, mask_secret
from app.models.endpoint import Endpoint, Environment, Tag
from app.monitoring.validators import (
    UrlValidationError,
    clamp_interval,
    clamp_timeout,
    normalise_status_codes,
    parse_target,
)

logger = get_logger(__name__)

# Headers an operator must not set through custom_headers: they either break
# the check or belong to the authentication settings.
_RESERVED_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}

SORTABLE_FIELDS = {
    "name": Endpoint.name,
    "url": Endpoint.url,
    "hostname": Endpoint.hostname,
    "status": Endpoint.current_status,
    "last_checked_at": Endpoint.last_checked_at,
    "response_time": Endpoint.last_response_time_ms,
    "ssl_expires_at": Endpoint.ssl_expires_at,
    "ssl_days_remaining": Endpoint.ssl_days_remaining,
    "ssl_status": Endpoint.ssl_status,
    "created_at": Endpoint.created_at,
    "updated_at": Endpoint.updated_at,
    "owner": Endpoint.owner,
    "team": Endpoint.team,
    "interval": Endpoint.interval_seconds,
}


class EndpointConflict(ValueError):
    """A duplicate endpoint already exists."""


def base_query() -> Select:
    """Endpoint select with tags and environment eagerly loaded."""
    return select(Endpoint).options(
        selectinload(Endpoint.tags), selectinload(Endpoint.environment)
    )


# ----------------------------------------------------- tags & environments
async def resolve_tags(
    session: AsyncSession,
    names: Iterable[str] | None,
    *,
    create_missing: bool = True,
) -> list[Tag]:
    """Map tag names onto rows, creating any that do not exist yet.

    Tags are free-form by design - teams name things their own way - so an
    unknown name is a creation, not an error.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        if len(name) > 64:
            raise ValueError(f"tag '{name[:20]}...' exceeds 64 characters")
        seen.add(name)
        cleaned.append(name)

    if not cleaned:
        return []

    existing = list(
        (await session.execute(select(Tag).where(Tag.name.in_(cleaned)))).scalars().all()
    )
    by_name = {tag.name: tag for tag in existing}

    for name in cleaned:
        if name in by_name:
            continue
        if not create_missing:
            raise ValueError(f"unknown tag '{name}'")
        tag = Tag(name=name)
        session.add(tag)
        by_name[name] = tag

    await session.flush()
    return [by_name[name] for name in cleaned]


async def resolve_environment(
    session: AsyncSession,
    name_or_id: str | uuid.UUID | None,
    *,
    create_missing: bool = False,
) -> Environment | None:
    if name_or_id in (None, ""):
        return None

    if isinstance(name_or_id, uuid.UUID):
        return (
            await session.execute(
                select(Environment).where(Environment.id == name_or_id)
            )
        ).scalar_one_or_none()

    raw = str(name_or_id).strip()
    try:
        as_uuid = uuid.UUID(raw)
    except (ValueError, AttributeError):
        as_uuid = None
    if as_uuid is not None:
        found = (
            await session.execute(select(Environment).where(Environment.id == as_uuid))
        ).scalar_one_or_none()
        if found:
            return found

    name = raw.lower()
    found = (
        await session.execute(select(Environment).where(Environment.name == name))
    ).scalar_one_or_none()
    if found or not create_missing:
        return found

    environment = Environment(name=name, display_name=raw.title())
    session.add(environment)
    await session.flush()
    logger.info("environment_auto_created", name=name)
    return environment


# -------------------------------------------------------------- validation
def validate_custom_headers(headers: dict[str, Any] | None) -> dict[str, str] | None:
    if not headers:
        return None
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).strip()
        if not name:
            continue
        if name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"header '{name}' cannot be overridden")
        if name.lower() == "authorization":
            raise ValueError(
                "set Authorization through the authentication settings so the "
                "credential is stored encrypted"
            )
        if len(name) > 128:
            raise ValueError(f"header name '{name[:20]}...' is too long")
        text = str(value)
        if len(text) > 1024:
            raise ValueError(f"value for header '{name}' is too long")
        cleaned[name] = text
    if len(cleaned) > 25:
        raise ValueError("at most 25 custom headers are supported")
    return cleaned or None


def validate_auth(
    auth_type: str,
    *,
    username: str | None,
    header_name: str | None,
    secret: str | None,
    has_existing_secret: bool,
) -> None:
    if auth_type == AuthType.NONE.value:
        return
    if auth_type not in {t.value for t in AuthType}:
        raise ValueError(f"unsupported authentication type '{auth_type}'")
    if not secret and not has_existing_secret:
        raise ValueError(f"{auth_type} authentication requires a credential")
    if auth_type == AuthType.BASIC.value and not username:
        raise ValueError("basic authentication requires a username")
    if auth_type == AuthType.HEADER.value and not header_name:
        raise ValueError("custom header authentication requires a header name")


async def assert_unique(
    session: AsyncSession,
    *,
    url: str,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Reject duplicates on URL or name.

    Both matter: two rows with the same URL double the load on the monitored
    host, and two rows with the same name make every dashboard ambiguous.
    """
    stmt = select(Endpoint.id, Endpoint.name, Endpoint.url).where(
        or_(
            func.lower(Endpoint.url) == url.lower(),
            func.lower(Endpoint.name) == name.strip().lower(),
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(Endpoint.id != exclude_id)
    row = (await session.execute(stmt.limit(1))).first()
    if row is None:
        return
    if row[2].lower() == url.lower():
        raise EndpointConflict(f"an endpoint with URL '{url}' already exists")
    raise EndpointConflict(f"an endpoint named '{name}' already exists")


# ------------------------------------------------------------------ writes
async def create_endpoint(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
    created_by_id: uuid.UUID | None = None,
    check_unique: bool = True,
) -> Endpoint:
    """Create one endpoint from a validated payload dict."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 160:
        raise ValueError("name must be at most 160 characters")

    check_type = str(payload.get("check_type") or CheckType.HTTP.value).lower()
    if check_type not in {t.value for t in CheckType}:
        raise ValueError(f"unsupported check type '{check_type}'")

    target = parse_target(str(payload.get("url") or ""))
    # An explicit port overrides the one parsed from the URL.
    port = payload.get("port")
    if port not in (None, ""):
        port = int(port)
        if not 1 <= port <= 65535:
            raise UrlValidationError("port must be between 1 and 65535")
    else:
        port = target.port

    if check_unique:
        await assert_unique(session, url=target.url, name=name)

    interval = clamp_interval(
        payload.get("interval_seconds") or config.get("default_monitor_interval")
    )
    timeout = clamp_timeout(
        payload.get("timeout_seconds") or config.get("default_timeout"),
        interval=interval,
    )

    environment = await resolve_environment(
        session, payload.get("environment"), create_missing=bool(payload.get(
            "create_missing_environment", False
        ))
    )
    if environment is None and payload.get("environment_id"):
        environment = await resolve_environment(session, payload["environment_id"])

    tags = await resolve_tags(session, payload.get("tags"))

    secret = payload.get("auth_secret")
    auth_type = str(payload.get("auth_type") or AuthType.NONE.value).lower()
    validate_auth(
        auth_type,
        username=payload.get("auth_username"),
        header_name=payload.get("auth_header_name"),
        secret=secret,
        has_existing_secret=False,
    )

    endpoint = Endpoint(
        name=name,
        url=target.url,
        protocol=target.protocol,
        hostname=target.hostname,
        port=port,
        path=target.path,
        check_type=check_type,
        http_method=str(payload.get("http_method") or "GET").upper(),
        environment_id=environment.id if environment else None,
        description=payload.get("description"),
        owner=_trim(payload.get("owner"), 128),
        team=_trim(payload.get("team"), 128),
        application=_trim(payload.get("application"), 128),
        monitoring_enabled=_as_bool(payload.get("monitoring_enabled"), True),
        is_paused=_as_bool(payload.get("is_paused"), False),
        interval_seconds=interval,
        timeout_seconds=timeout,
        expected_status_codes=normalise_status_codes(
            payload.get("expected_status_codes")
        ),
        expected_body_substring=_trim(payload.get("expected_body_substring"), 255),
        follow_redirects=_as_bool(payload.get("follow_redirects"), True),
        verify_ssl=_as_bool(payload.get("verify_ssl"), True),
        ssl_monitoring_enabled=_as_bool(payload.get("ssl_monitoring_enabled"), True)
        and target.protocol == "https",
        request_body=payload.get("request_body"),
        custom_headers=validate_custom_headers(payload.get("custom_headers")),
        auth_type=auth_type,
        auth_username=_trim(payload.get("auth_username"), 128),
        auth_header_name=_trim(payload.get("auth_header_name"), 128),
        failure_threshold=int(
            payload.get("failure_threshold") or config.get("failure_threshold", 3)
        ),
        response_time_threshold_ms=_as_int(payload.get("response_time_threshold_ms")),
        ssl_warning_days=_as_int(payload.get("ssl_warning_days")),
        ssl_critical_days=_as_int(payload.get("ssl_critical_days")),
        alerts_enabled=_as_bool(payload.get("alerts_enabled"), True),
        current_status=EndpointStatus.UNKNOWN.value,
        ssl_status=(
            SslStatus.UNABLE_TO_CHECK.value
            if target.protocol == "https"
            else SslStatus.NOT_APPLICABLE.value
        ),
        created_by_id=created_by_id,
        updated_by_id=created_by_id,
        # Due immediately so a newly added endpoint shows a real status within
        # one worker poll rather than after a full interval.
        next_check_at=datetime.now(timezone.utc),
    )
    if secret:
        endpoint.auth_secret_encrypted = encrypt_secret(str(secret))
        endpoint.auth_secret_hint = mask_secret(str(secret))
    endpoint.tags = tags

    session.add(endpoint)
    await session.flush()
    return endpoint


async def update_endpoint(
    session: AsyncSession,
    endpoint: Endpoint,
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
    updated_by_id: uuid.UUID | None = None,
) -> tuple[Endpoint, dict[str, Any]]:
    """Apply a partial update. Returns the endpoint and a before/after diff."""
    before = snapshot(endpoint)

    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise ValueError("name must not be empty")
        endpoint.name = name[:160]

    if "url" in payload and payload["url"]:
        target = parse_target(str(payload["url"]))
        endpoint.url = target.url
        endpoint.protocol = target.protocol
        endpoint.hostname = target.hostname
        endpoint.port = target.port
        endpoint.path = target.path
        if target.protocol != "https":
            endpoint.ssl_monitoring_enabled = False
            endpoint.ssl_status = SslStatus.NOT_APPLICABLE.value

    if payload.get("port") not in (None, ""):
        port = int(payload["port"])
        if not 1 <= port <= 65535:
            raise UrlValidationError("port must be between 1 and 65535")
        endpoint.port = port

    if endpoint.name != before["name"] or endpoint.url != before["url"]:
        await assert_unique(
            session, url=endpoint.url, name=endpoint.name, exclude_id=endpoint.id
        )

    if "check_type" in payload and payload["check_type"]:
        check_type = str(payload["check_type"]).lower()
        if check_type not in {t.value for t in CheckType}:
            raise ValueError(f"unsupported check type '{check_type}'")
        endpoint.check_type = check_type

    if "http_method" in payload and payload["http_method"]:
        endpoint.http_method = str(payload["http_method"]).upper()

    for field, caster, limit in (
        ("description", str, None),
        ("owner", str, 128),
        ("team", str, 128),
        ("application", str, 128),
        ("expected_body_substring", str, 255),
        ("request_body", str, None),
    ):
        if field in payload:
            value = payload[field]
            setattr(
                endpoint,
                field,
                None if value in (None, "") else _trim(caster(value), limit),
            )

    for field in (
        "monitoring_enabled",
        "is_paused",
        "follow_redirects",
        "verify_ssl",
        "alerts_enabled",
    ):
        if field in payload and payload[field] is not None:
            setattr(endpoint, field, bool(payload[field]))

    # A manual resume clears any deployment pause note, so the row never claims
    # to be paused by a change that has since let go of it.
    if not endpoint.is_paused:
        endpoint.pause_reason = None
        endpoint.paused_by_change_id = None

    if "ssl_monitoring_enabled" in payload and payload["ssl_monitoring_enabled"] is not None:
        endpoint.ssl_monitoring_enabled = (
            bool(payload["ssl_monitoring_enabled"]) and endpoint.protocol == "https"
        )

    if "interval_seconds" in payload and payload["interval_seconds"]:
        endpoint.interval_seconds = clamp_interval(payload["interval_seconds"])
    if "timeout_seconds" in payload and payload["timeout_seconds"]:
        endpoint.timeout_seconds = clamp_timeout(
            payload["timeout_seconds"], interval=endpoint.interval_seconds
        )
    else:
        endpoint.timeout_seconds = clamp_timeout(
            endpoint.timeout_seconds, interval=endpoint.interval_seconds
        )

    if "expected_status_codes" in payload and payload["expected_status_codes"]:
        endpoint.expected_status_codes = normalise_status_codes(
            payload["expected_status_codes"]
        )

    for field in (
        "failure_threshold",
        "response_time_threshold_ms",
        "ssl_warning_days",
        "ssl_critical_days",
    ):
        if field in payload:
            endpoint_value = _as_int(payload[field])
            if field == "failure_threshold" and endpoint_value is None:
                endpoint_value = int(config.get("failure_threshold", 3))
            setattr(endpoint, field, endpoint_value)

    if "custom_headers" in payload:
        endpoint.custom_headers = validate_custom_headers(payload["custom_headers"])

    if "environment" in payload or "environment_id" in payload:
        key = payload.get("environment_id", payload.get("environment"))
        environment = await resolve_environment(session, key)
        endpoint.environment_id = environment.id if environment else None

    if "tags" in payload and payload["tags"] is not None:
        endpoint.tags = await resolve_tags(session, payload["tags"])

    # ------------------------------------------------------ credentials
    auth_type = str(
        payload.get("auth_type", endpoint.auth_type) or AuthType.NONE.value
    ).lower()
    secret = payload.get("auth_secret")
    if "auth_username" in payload:
        endpoint.auth_username = _trim(payload["auth_username"], 128)
    if "auth_header_name" in payload:
        endpoint.auth_header_name = _trim(payload["auth_header_name"], 128)

    if auth_type == AuthType.NONE.value:
        endpoint.auth_type = auth_type
        endpoint.auth_secret_encrypted = None
        endpoint.auth_secret_hint = None
    else:
        validate_auth(
            auth_type,
            username=endpoint.auth_username,
            header_name=endpoint.auth_header_name,
            secret=secret,
            has_existing_secret=bool(endpoint.auth_secret_encrypted),
        )
        endpoint.auth_type = auth_type
        # An absent auth_secret means "leave the stored credential alone" -
        # the UI never receives the plaintext, so it cannot echo it back.
        if secret:
            endpoint.auth_secret_encrypted = encrypt_secret(str(secret))
            endpoint.auth_secret_hint = mask_secret(str(secret))

    endpoint.updated_by_id = updated_by_id

    # Re-arm the schedule so configuration changes take effect promptly rather
    # than at the end of the previous interval.
    endpoint.next_check_at = datetime.now(timezone.utc)
    endpoint.lease_expires_at = None
    endpoint.leased_by = None

    await session.flush()
    after = snapshot(endpoint)
    changes = {
        key: {"from": before[key], "to": after[key]}
        for key in after
        if before.get(key) != after[key]
    }
    return endpoint, changes


def snapshot(endpoint: Endpoint) -> dict[str, Any]:
    """Audit-friendly view of an endpoint. Never includes the credential."""
    return {
        "name": endpoint.name,
        "url": endpoint.url,
        "check_type": endpoint.check_type,
        "http_method": endpoint.http_method,
        "port": endpoint.port,
        "environment_id": str(endpoint.environment_id)
        if endpoint.environment_id
        else None,
        "tags": sorted(endpoint.tag_names),
        "description": endpoint.description,
        "owner": endpoint.owner,
        "team": endpoint.team,
        "application": endpoint.application,
        "monitoring_enabled": endpoint.monitoring_enabled,
        "is_paused": endpoint.is_paused,
        "interval_seconds": endpoint.interval_seconds,
        "timeout_seconds": endpoint.timeout_seconds,
        "expected_status_codes": endpoint.expected_status_codes,
        "expected_body_substring": endpoint.expected_body_substring,
        "follow_redirects": endpoint.follow_redirects,
        "verify_ssl": endpoint.verify_ssl,
        "ssl_monitoring_enabled": endpoint.ssl_monitoring_enabled,
        "custom_header_names": sorted((endpoint.custom_headers or {}).keys()),
        "auth_type": endpoint.auth_type,
        "auth_username": endpoint.auth_username,
        "auth_header_name": endpoint.auth_header_name,
        "has_auth_secret": bool(endpoint.auth_secret_encrypted),
        "failure_threshold": endpoint.failure_threshold,
        "response_time_threshold_ms": endpoint.response_time_threshold_ms,
        "ssl_warning_days": endpoint.ssl_warning_days,
        "ssl_critical_days": endpoint.ssl_critical_days,
        "alerts_enabled": endpoint.alerts_enabled,
    }


# ------------------------------------------------------------------ search
def apply_search(stmt: Select, term: str | None) -> Select:
    """Free-text search across name, URL, hostname, description, owner, team."""
    if not term:
        return stmt
    needle = f"%{term.strip().lower()}%"
    return stmt.where(
        or_(
            func.lower(Endpoint.name).like(needle),
            func.lower(Endpoint.url).like(needle),
            func.lower(Endpoint.hostname).like(needle),
            func.lower(func.coalesce(Endpoint.description, "")).like(needle),
            func.lower(func.coalesce(Endpoint.owner, "")).like(needle),
            func.lower(func.coalesce(Endpoint.team, "")).like(needle),
            func.lower(func.coalesce(Endpoint.application, "")).like(needle),
        )
    )


def apply_sort(stmt: Select, sort_by: str | None, direction: str = "asc") -> Select:
    column = SORTABLE_FIELDS.get((sort_by or "name").lower(), Endpoint.name)
    ordering = column.desc() if str(direction).lower() == "desc" else column.asc()
    # NULLs last in both directions so "never checked" rows do not squat at
    # the top of a latency sort.
    return stmt.order_by(ordering.nulls_last(), Endpoint.name.asc())


def apply_filters(
    stmt: Select,
    *,
    environment_ids: Sequence[uuid.UUID] | None = None,
    tag_ids: Sequence[uuid.UUID] | None = None,
    statuses: Sequence[str] | None = None,
    ssl_statuses: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    teams: Sequence[str] | None = None,
    applications: Sequence[str] | None = None,
    monitoring_enabled: bool | None = None,
    check_types: Sequence[str] | None = None,
    protocols: Sequence[str] | None = None,
    ssl_expiring_within_days: int | None = None,
) -> Select:
    from app.models.endpoint import endpoint_tags  # local import avoids a cycle

    if environment_ids:
        stmt = stmt.where(Endpoint.environment_id.in_(list(environment_ids)))
    if statuses:
        stmt = stmt.where(Endpoint.current_status.in_(list(statuses)))
    if ssl_statuses:
        stmt = stmt.where(Endpoint.ssl_status.in_(list(ssl_statuses)))
    if owners:
        stmt = stmt.where(Endpoint.owner.in_(list(owners)))
    if teams:
        stmt = stmt.where(Endpoint.team.in_(list(teams)))
    if applications:
        stmt = stmt.where(Endpoint.application.in_(list(applications)))
    if check_types:
        stmt = stmt.where(Endpoint.check_type.in_(list(check_types)))
    if protocols:
        stmt = stmt.where(Endpoint.protocol.in_(list(protocols)))
    if monitoring_enabled is not None:
        stmt = stmt.where(Endpoint.monitoring_enabled.is_(bool(monitoring_enabled)))
    if ssl_expiring_within_days is not None:
        stmt = stmt.where(
            Endpoint.ssl_days_remaining.isnot(None),
            Endpoint.ssl_days_remaining <= int(ssl_expiring_within_days),
        )
    if tag_ids:
        stmt = stmt.where(
            select(endpoint_tags.c.endpoint_id)
            .where(
                endpoint_tags.c.endpoint_id == Endpoint.id,
                endpoint_tags.c.tag_id.in_(list(tag_ids)),
            )
            .exists()
        )
    return stmt


async def count_query(session: AsyncSession, stmt: Select) -> int:
    """Total rows for a filtered endpoint query, for pagination metadata."""
    subquery = stmt.with_only_columns(Endpoint.id).order_by(None).subquery()
    return int(
        (await session.execute(select(func.count()).select_from(subquery))).scalar() or 0
    )


async def get_endpoint(
    session: AsyncSession, endpoint_id: uuid.UUID
) -> Endpoint | None:
    return (
        await session.execute(base_query().where(Endpoint.id == endpoint_id))
    ).scalar_one_or_none()


async def distinct_values(session: AsyncSession, field: str) -> list[str]:
    """Distinct owners/teams/applications, for filter dropdowns."""
    if field not in ("owner", "team", "application"):
        raise ValueError(f"cannot enumerate '{field}'")
    column = getattr(Endpoint, field)
    rows = (
        await session.execute(
            select(column).where(column.isnot(None)).distinct().order_by(column)
        )
    ).scalars().all()
    return [row for row in rows if row]


# ----------------------------------------------------------------- helpers
def _trim(value: Any, limit: int | None) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"'{value}' is not a valid number") from None
