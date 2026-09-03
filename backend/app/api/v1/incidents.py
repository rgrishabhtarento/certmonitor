"""Incident history and alert management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload, selectinload

from app.api.deps import (
    DbSession,
    Pagination,
    ReadAlerts,
    ReadIncidents,
    WriteAlerts,
    WriteIncidents,
    parse_uuid_list,
    split_csv_param,
)
from app.core.enums import AuditAction, IncidentStatus
from app.models.alert import Alert
from app.models.endpoint import Endpoint, endpoint_tags
from app.models.incident import Incident
from app.models.user import User
from app.schemas.common import BulkActionResult, Message, Page
from app.schemas.monitoring import (
    AlertAcknowledge,
    AlertRead,
    IncidentRead,
    IncidentUpdate,
    incident_to_schema,
)
from app.services import audit_service, monitoring_service

router = APIRouter(tags=["Incidents & Alerts"])


def _incident_to_schema(
    incident: Incident, *, acknowledged_by: str | None = None
) -> IncidentRead:
    return incident_to_schema(
        incident,
        reason_label=monitoring_service.humanise_reason(incident.reason),
        acknowledged_by=acknowledged_by,
    )


def _incident_query():
    return select(Incident).options(
        joinedload(Incident.endpoint).selectinload(Endpoint.tags),
        joinedload(Incident.endpoint).joinedload(Endpoint.environment),
    )


@router.get(
    "/incidents",
    response_model=Page[IncidentRead],
    summary="Incident history",
)
async def list_incidents(
    session: DbSession,
    _user: ReadIncidents,
    page: Pagination,
    endpoint_id: Annotated[uuid.UUID | None, Query()] = None,
    incident_status: Annotated[
        list[str] | None, Query(alias="status", description="open or resolved")
    ] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    reason: Annotated[list[str] | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    min_duration_seconds: Annotated[int | None, Query(ge=0)] = None,
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> Page[IncidentRead]:
    """Paginated incidents, newest first by default.

    One incident spans a continuous outage regardless of how many individual
    checks failed inside it - ``failed_check_count`` reports that number.
    """
    stmt = _incident_query().join(Endpoint, Endpoint.id == Incident.endpoint_id)

    if endpoint_id is not None:
        stmt = stmt.where(Incident.endpoint_id == endpoint_id)
    statuses = split_csv_param(incident_status)
    if statuses:
        stmt = stmt.where(Incident.status.in_(statuses))
    severities = split_csv_param(severity)
    if severities:
        stmt = stmt.where(Incident.severity.in_(severities))
    reasons = split_csv_param(reason)
    if reasons:
        stmt = stmt.where(Incident.reason.in_(reasons))
    environment_ids = parse_uuid_list(environment)
    if environment_ids:
        stmt = stmt.where(Endpoint.environment_id.in_(environment_ids))
    tag_ids = parse_uuid_list(tag)
    if tag_ids:
        stmt = stmt.where(
            select(endpoint_tags.c.endpoint_id)
            .where(
                endpoint_tags.c.endpoint_id == Endpoint.id,
                endpoint_tags.c.tag_id.in_(tag_ids),
            )
            .exists()
        )
    if since is not None:
        stmt = stmt.where(Incident.started_at >= since)
    if until is not None:
        stmt = stmt.where(Incident.started_at <= until)
    if min_duration_seconds is not None:
        stmt = stmt.where(
            Incident.duration_seconds.isnot(None),
            Incident.duration_seconds >= min_duration_seconds,
        )
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Endpoint.name).like(needle)
            | func.lower(Endpoint.url).like(needle)
            | func.lower(func.coalesce(Incident.error_message, "")).like(needle)
        )

    count_stmt = stmt.with_only_columns(func.count(Incident.id)).order_by(None)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    ordering = (
        Incident.started_at.asc() if sort_dir == "asc" else Incident.started_at.desc()
    )
    rows = list(
        (
            await session.execute(
                stmt.order_by(ordering).limit(page.page_size).offset(page.offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    return Page.build(
        [_incident_to_schema(row) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/incidents/{incident_id}",
    response_model=IncidentRead,
    summary="Incident details",
)
async def get_incident(
    incident_id: int, session: DbSession, _user: ReadIncidents
) -> IncidentRead:
    incident = (
        await session.execute(_incident_query().where(Incident.id == incident_id))
    ).scalars().unique().one_or_none()
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found."
        )

    acknowledged_by = None
    if incident.acknowledged_by_id:
        acknowledged_by = (
            await session.execute(
                select(User.username).where(User.id == incident.acknowledged_by_id)
            )
        ).scalar()
    return _incident_to_schema(incident, acknowledged_by=acknowledged_by)


@router.patch(
    "/incidents/{incident_id}",
    response_model=IncidentRead,
    summary="Acknowledge an incident or attach notes",
)
async def update_incident(
    incident_id: int,
    payload: IncidentUpdate,
    user: WriteIncidents,
    request: Request,
    session: DbSession,
) -> IncidentRead:
    """Annotate an incident.

    Incidents are opened and closed by the monitoring worker from observed
    state; a human can acknowledge one and record why it happened, but cannot
    declare it resolved by hand.
    """
    incident = (
        await session.execute(_incident_query().where(Incident.id == incident_id))
    ).scalars().unique().one_or_none()
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found."
        )

    changed: dict[str, object] = {}
    if payload.notes is not None:
        incident.notes = payload.notes
        changed["notes"] = "updated"
    if payload.acknowledge is not None:
        if payload.acknowledge:
            incident.acknowledged_by_id = user.id
            incident.acknowledged_at = datetime.now(timezone.utc)
        else:
            incident.acknowledged_by_id = None
            incident.acknowledged_at = None
        changed["acknowledged"] = payload.acknowledge

    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_UPDATED.value,
        user=user,
        resource_type="incident",
        resource_id=incident.id,
        resource_name=incident.endpoint.name if incident.endpoint else None,
        details=changed,
        request=request,
    )
    await session.commit()
    await session.refresh(incident)
    return _incident_to_schema(
        incident,
        acknowledged_by=user.username if incident.acknowledged_by_id else None,
    )


# ----------------------------------------------------------------- alerts
def _alert_to_schema(alert: Alert, *, acknowledged_by: str | None = None) -> AlertRead:
    model = AlertRead.model_validate(alert)
    model.acknowledged_by = acknowledged_by
    if alert.endpoint is not None:
        model.endpoint_name = alert.endpoint.name
        model.endpoint_url = alert.endpoint.url
    return model


@router.get("/alerts", response_model=Page[AlertRead], summary="Alert history")
async def list_alerts(
    session: DbSession,
    _user: ReadAlerts,
    page: Pagination,
    endpoint_id: Annotated[uuid.UUID | None, Query()] = None,
    alert_type: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    acknowledged: Annotated[bool | None, Query()] = None,
    notification_status: Annotated[list[str] | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
) -> Page[AlertRead]:
    stmt = select(Alert).options(
        joinedload(Alert.endpoint).selectinload(Endpoint.tags)
    )

    if endpoint_id is not None:
        stmt = stmt.where(Alert.endpoint_id == endpoint_id)
    types = split_csv_param(alert_type)
    if types:
        stmt = stmt.where(Alert.alert_type.in_(types))
    severities = split_csv_param(severity)
    if severities:
        stmt = stmt.where(Alert.severity.in_(severities))
    if acknowledged is not None:
        stmt = stmt.where(Alert.is_acknowledged.is_(acknowledged))
    statuses = split_csv_param(notification_status)
    if statuses:
        stmt = stmt.where(Alert.notification_status.in_(statuses))
    if since is not None:
        stmt = stmt.where(Alert.created_at >= since)
    if until is not None:
        stmt = stmt.where(Alert.created_at <= until)
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Alert.title).like(needle)
            | func.lower(func.coalesce(Alert.message, "")).like(needle)
        )

    count_stmt = stmt.with_only_columns(func.count(Alert.id)).order_by(None)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    rows = list(
        (
            await session.execute(
                stmt.order_by(Alert.created_at.desc())
                .limit(page.page_size)
                .offset(page.offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return Page.build(
        [_alert_to_schema(row) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/alerts/unacknowledged/count",
    response_model=dict,
    summary="Unacknowledged alert counts by severity (for the nav badge)",
)
async def unacknowledged_counts(session: DbSession, _user: ReadAlerts) -> dict:
    rows = (
        await session.execute(
            select(Alert.severity, func.count(Alert.id))
            .where(Alert.is_acknowledged.is_(False))
            .group_by(Alert.severity)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts


@router.post(
    "/alerts/acknowledge",
    response_model=BulkActionResult,
    summary="Acknowledge one or more alerts",
)
async def acknowledge_alerts(
    payload: AlertAcknowledge,
    user: WriteAlerts,
    request: Request,
    session: DbSession,
) -> BulkActionResult:
    """Mark alerts as seen.

    Passing no ids acknowledges every currently unacknowledged alert, which is
    what an operator wants after working through a backlog.
    """
    stmt = update(Alert).where(Alert.is_acknowledged.is_(False))
    requested = 0
    if payload.alert_ids:
        requested = len(payload.alert_ids)
        stmt = stmt.where(Alert.id.in_(payload.alert_ids))
    else:
        requested = int(
            (
                await session.execute(
                    select(func.count(Alert.id)).where(
                        Alert.is_acknowledged.is_(False)
                    )
                )
            ).scalar()
            or 0
        )

    result = await session.execute(
        stmt.values(
            is_acknowledged=True,
            acknowledged_by_id=user.id,
            acknowledged_at=datetime.now(timezone.utc),
        )
    )
    updated = int(result.rowcount or 0)

    await audit_service.record(
        session,
        action=AuditAction.ALERT_ACKNOWLEDGED.value,
        user=user,
        resource_type="alert",
        details={"requested": requested, "acknowledged": updated},
        request=request,
    )
    await session.commit()
    return BulkActionResult(
        requested=requested, succeeded=updated, failed=max(0, requested - updated)
    )


@router.delete(
    "/alerts/{alert_id}", response_model=Message, summary="Delete an alert"
)
async def delete_alert(
    alert_id: int, user: WriteAlerts, request: Request, session: DbSession
) -> Message:
    alert = (
        await session.execute(select(Alert).where(Alert.id == alert_id))
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found."
        )
    await session.delete(alert)
    await audit_service.record(
        session,
        action=AuditAction.ALERT_ACKNOWLEDGED.value,
        user=user,
        resource_type="alert",
        resource_id=alert_id,
        details={"deleted": True},
        request=request,
    )
    await session.commit()
    return Message(detail="Alert deleted.")
