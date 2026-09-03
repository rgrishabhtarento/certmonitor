"""Runtime configuration, audit log access and notification channels."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    DbSession,
    ReadAudit,
    ReadSettings,
    RuntimeConfig,
    WriteNotifications,
    WriteSettings,
    split_csv_param,
)
from app.core.enums import (
    AlertType,
    AuditAction,
    NotificationChannelType,
    Severity,
)
from app.core.logging import get_logger
from app.models.alert import NotificationChannel
from app.models.system import AuditLog, SystemSetting
from app.schemas.common import Message, Page
from app.schemas.dashboard import SettingRead, SettingsResponse, SettingsUpdate
from app.schemas.monitoring import (
    AuditLogRead,
    NotificationChannelRead,
    NotificationChannelUpdate,
    NotificationChannelWrite,
)
from app.services import (
    audit_service,
    monitoring_service,
    notification_service,
    retention_service,
    settings_service,
)

logger = get_logger(__name__)

router = APIRouter(tags=["Settings"])


# --------------------------------------------------------------- settings
@router.get(
    "/settings",
    response_model=SettingsResponse,
    summary="Monitoring configuration and its current effective values",
)
async def get_settings(
    session: DbSession, _user: ReadSettings
) -> SettingsResponse:
    """Return the editable settings plus what is currently in force.

    ``effective`` is the merged view the worker actually reads, which is what
    an operator needs to confirm a change took hold.
    """
    rows = (
        await session.execute(select(SystemSetting).order_by(SystemSetting.category))
    ).scalars().all()
    effective = await settings_service.load_settings(session, use_cache=False)

    by_key = {row.key: row for row in rows}
    items: list[SettingRead] = []
    for spec in settings_service.SETTING_SPECS:
        row = by_key.get(spec.key)
        items.append(
            SettingRead(
                key=spec.key,
                value=effective.get(spec.key, spec.default),
                value_type=spec.value_type,
                category=spec.category,
                label=spec.label,
                description=spec.description,
                allowed_values=spec.allowed_values,
                min_value=spec.min_value,
                max_value=spec.max_value,
                is_editable=spec.is_editable,
                updated_at=row.updated_at if row else None,
            )
        )

    return SettingsResponse(
        settings=items,
        effective=effective,
        storage=await retention_service.storage_estimate(session),
    )


@router.put(
    "/settings",
    response_model=SettingsResponse,
    summary="Update monitoring configuration",
)
async def update_settings(
    payload: SettingsUpdate,
    user: WriteSettings,
    request: Request,
    session: DbSession,
) -> SettingsResponse:
    """Apply configuration changes.

    All values are validated before anything is written, so a single bad value
    rejects the whole batch. Changing an SSL threshold re-grades every stored
    certificate immediately rather than waiting for each endpoint's next
    check.
    """
    before = await settings_service.load_settings(session, use_cache=False)
    try:
        applied = await settings_service.update_settings(
            session, payload.updates, user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    ssl_changed = any(
        key in applied for key in ("ssl_warning_days", "ssl_critical_days")
    )
    if ssl_changed:
        after = await settings_service.load_settings(session, use_cache=False)
        regraded = await monitoring_service.regrade_certificates(
            session,
            warning_days=int(after["ssl_warning_days"]),
            critical_days=int(after["ssl_critical_days"]),
        )
        logger.info("ssl_thresholds_changed", regraded=regraded)

    await audit_service.record(
        session,
        action=AuditAction.SETTINGS_CHANGED.value,
        user=user,
        resource_type="settings",
        details={
            "changes": {
                key: {"from": before.get(key), "to": value}
                for key, value in applied.items()
            }
        },
        request=request,
    )
    await session.commit()
    return await get_settings(session, user)  # type: ignore[arg-type]


@router.get(
    "/settings/alert-options",
    response_model=dict,
    summary="Enumerations the settings and alert screens need",
)
async def alert_options(config: RuntimeConfig) -> dict:
    return {
        "alert_types": [t.value for t in AlertType],
        "severities": [s.value for s in Severity],
        "channel_types": [c.value for c in NotificationChannelType],
        "audit_actions": [a.value for a in AuditAction],
        "allowed_intervals": config.get("allowed_intervals", []),
    }


# ------------------------------------------------------------- audit logs
@router.get(
    "/audit-logs",
    response_model=Page[AuditLogRead],
    summary="Administrative audit trail",
)
async def list_audit_logs(
    session: DbSession,
    _user: ReadAudit,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    action: Annotated[list[str] | None, Query()] = None,
    username: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    log_status: Annotated[list[str] | None, Query(alias="status")] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
) -> Page[AuditLogRead]:
    stmt = select(AuditLog)

    actions = split_csv_param(action)
    if actions:
        stmt = stmt.where(AuditLog.action.in_(actions))
    if username:
        stmt = stmt.where(func.lower(AuditLog.username) == username.strip().lower())
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    statuses = split_csv_param(log_status)
    if statuses:
        stmt = stmt.where(AuditLog.status.in_(statuses))
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= until)
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(func.coalesce(AuditLog.resource_name, "")).like(needle),
                func.lower(func.coalesce(AuditLog.username, "")).like(needle),
                func.lower(AuditLog.action).like(needle),
                func.lower(func.coalesce(AuditLog.ip_address, "")).like(needle),
            )
        )

    total = int(
        (
            await session.execute(
                stmt.with_only_columns(func.count(AuditLog.id)).order_by(None)
            )
        ).scalar()
        or 0
    )
    rows = list(
        (
            await session.execute(
                stmt.order_by(AuditLog.created_at.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build(
        [AuditLogRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/audit-logs/actions",
    response_model=list[str],
    summary="Distinct actions present in the audit log",
)
async def audit_actions(session: DbSession, _user: ReadAudit) -> list[str]:
    rows = (
        await session.execute(
            select(AuditLog.action).distinct().order_by(AuditLog.action)
        )
    ).scalars().all()
    return list(rows)


# -------------------------------------------------- notification channels
def _channel_to_schema(channel: NotificationChannel) -> NotificationChannelRead:
    return NotificationChannelRead.model_validate(channel)


@router.get(
    "/notification-channels",
    response_model=list[NotificationChannelRead],
    summary="List notification channels",
)
async def list_channels(
    session: DbSession, _user: ReadSettings
) -> list[NotificationChannelRead]:
    """Channels, with only their non-sensitive configuration.

    Webhook URLs, SMTP passwords and routing keys are stored encrypted and are
    never returned; ``config_public`` carries just enough (host, port,
    recipient count) to identify a channel in the UI.
    """
    rows = (
        await session.execute(
            select(NotificationChannel).order_by(NotificationChannel.name)
        )
    ).scalars().all()
    return [_channel_to_schema(row) for row in rows]


@router.post(
    "/notification-channels",
    response_model=NotificationChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification channel",
)
async def create_channel(
    payload: NotificationChannelWrite,
    user: WriteNotifications,
    request: Request,
    session: DbSession,
) -> NotificationChannelRead:
    channel_type = payload.channel_type.strip().lower()
    try:
        config = notification_service.validate_config(channel_type, payload.config)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if payload.min_severity not in {s.value for s in Severity}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="min_severity must be info, warning or critical.",
        )
    unknown_events = [
        event
        for event in (payload.event_types or [])
        if event not in {t.value for t in AlertType}
    ]
    if unknown_events:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unknown event type(s): " + ", ".join(unknown_events),
        )

    channel = NotificationChannel(
        name=payload.name.strip(),
        channel_type=channel_type,
        is_enabled=payload.is_enabled,
        config_encrypted=notification_service.encrypt_config(config),
        config_public=notification_service.public_view(channel_type, config),
        min_severity=payload.min_severity,
        event_types=payload.event_types or None,
        environment_filter=payload.environment_filter or None,
        tag_filter=payload.tag_filter or None,
        created_by_id=user.id,
    )
    session.add(channel)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A channel named '{payload.name}' already exists.",
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.NOTIFICATION_CHANNEL_CREATED.value,
        user=user,
        resource_type="notification_channel",
        resource_id=channel.id,
        resource_name=channel.name,
        details={"channel_type": channel_type, "public": channel.config_public},
        request=request,
    )
    await session.commit()
    return _channel_to_schema(channel)


@router.put(
    "/notification-channels/{channel_id}",
    response_model=NotificationChannelRead,
    summary="Update a notification channel",
)
async def update_channel(
    channel_id: uuid.UUID,
    payload: NotificationChannelUpdate,
    user: WriteNotifications,
    request: Request,
    session: DbSession,
) -> NotificationChannelRead:
    """Update a channel.

    Omitting ``config`` leaves the stored credentials untouched, so an
    operator can retarget a channel's filters without re-entering secrets.
    """
    channel = (
        await session.execute(
            select(NotificationChannel).where(NotificationChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found."
        )

    if payload.name is not None:
        channel.name = payload.name.strip()
    if payload.is_enabled is not None:
        channel.is_enabled = payload.is_enabled
    if payload.min_severity is not None:
        if payload.min_severity not in {s.value for s in Severity}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="min_severity must be info, warning or critical.",
            )
        channel.min_severity = payload.min_severity
    if payload.event_types is not None:
        channel.event_types = payload.event_types or None
    if payload.environment_filter is not None:
        channel.environment_filter = payload.environment_filter or None
    if payload.tag_filter is not None:
        channel.tag_filter = payload.tag_filter or None

    if payload.config is not None:
        try:
            config = notification_service.validate_config(
                channel.channel_type, payload.config
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        channel.config_encrypted = notification_service.encrypt_config(config)
        channel.config_public = notification_service.public_view(
            channel.channel_type, config
        )

    await audit_service.record(
        session,
        action=AuditAction.NOTIFICATION_CHANNEL_UPDATED.value,
        user=user,
        resource_type="notification_channel",
        resource_id=channel.id,
        resource_name=channel.name,
        details={"config_replaced": payload.config is not None},
        request=request,
    )
    await session.commit()
    return _channel_to_schema(channel)


@router.post(
    "/notification-channels/{channel_id}/test",
    response_model=Message,
    summary="Send a test notification",
)
async def test_channel(
    channel_id: uuid.UUID,
    user: WriteNotifications,
    request: Request,
    session: DbSession,
) -> Message:
    channel = (
        await session.execute(
            select(NotificationChannel).where(NotificationChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found."
        )

    try:
        await notification_service.send_test_notification(channel)
    except Exception as exc:
        channel.last_error = str(exc)[:1000]
        channel.failure_count += 1
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Test notification failed: {exc}",
        ) from exc

    channel.last_error = None
    channel.success_count += 1
    await audit_service.record(
        session,
        action=AuditAction.NOTIFICATION_CHANNEL_UPDATED.value,
        user=user,
        resource_type="notification_channel",
        resource_id=channel.id,
        resource_name=channel.name,
        details={"test": "sent"},
        request=request,
    )
    await session.commit()
    return Message(detail=f"Test notification sent via '{channel.name}'.")


@router.delete(
    "/notification-channels/{channel_id}",
    response_model=Message,
    summary="Delete a notification channel",
)
async def delete_channel(
    channel_id: uuid.UUID,
    user: WriteNotifications,
    request: Request,
    session: DbSession,
) -> Message:
    channel = (
        await session.execute(
            select(NotificationChannel).where(NotificationChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found."
        )
    name = channel.name
    await session.delete(channel)
    await audit_service.record(
        session,
        action=AuditAction.NOTIFICATION_CHANNEL_DELETED.value,
        user=user,
        resource_type="notification_channel",
        resource_id=channel_id,
        resource_name=name,
        request=request,
    )
    await session.commit()
    return Message(detail=f"Channel '{name}' deleted.")
