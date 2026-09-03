"""Endpoint management, manual checks, history and per-endpoint statistics."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import (
    CheckEndpoints,
    DbSession,
    DeleteEndpoints,
    Pagination,
    ReadEndpoints,
    RuntimeConfig,
    WriteEndpoints,
    parse_uuid_list,
    split_csv_param,
)
from app.core.enums import AuditAction, EndpointStatus
from app.core.logging import get_logger
from app.models.endpoint import Endpoint, Environment, Tag
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate
from app.monitoring.validators import UrlValidationError
from app.schemas.common import BulkActionResult, Message, Page
from app.schemas.dashboard import EndpointStatsResponse, TimeSeriesPoint, WindowStats
from app.schemas.endpoint import (
    BulkEndpointAction,
    EndpointCreate,
    EndpointFilterOptions,
    EndpointListItem,
    EndpointRead,
    EndpointStatusUpdate,
    EndpointUpdate,
    EnvironmentRead,
    TagRead,
    endpoint_to_list_item,
    endpoint_to_read,
)
from app.schemas.monitoring import (
    CheckNowResponse,
    DiagnosticsResponse,
    MonitoringResultRead,
    SslCertificateRead,
)
from app.services import (
    audit_service,
    diagnostics_service,
    endpoint_service,
    monitoring_service,
    stats_service,
)
from app.services.endpoint_service import EndpointConflict

logger = get_logger(__name__)

router = APIRouter(prefix="/endpoints", tags=["Endpoints"])


# ----------------------------------------------------------------- helpers
async def _open_incident_ids(
    session, endpoint_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    if not endpoint_ids:
        return set()
    rows = (
        await session.execute(
            select(Incident.endpoint_id).where(
                Incident.endpoint_id.in_(endpoint_ids),
                Incident.status == "open",
            )
        )
    ).scalars().all()
    return set(rows)


async def _load_endpoint(session, endpoint_id: uuid.UUID) -> Endpoint:
    endpoint = await endpoint_service.get_endpoint(session, endpoint_id)
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found."
        )
    return endpoint


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# -------------------------------------------------------------------- list
@router.get(
    "",
    response_model=Page[EndpointListItem],
    summary="List endpoints with search, filtering, sorting and pagination",
)
async def list_endpoints(
    session: DbSession,
    _user: ReadEndpoints,
    page: Pagination,
    search: Annotated[
        str | None,
        Query(description="Matches name, URL, hostname, description, owner or team."),
    ] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    endpoint_status: Annotated[
        list[str] | None, Query(alias="status", description="up, down, degraded, unknown")
    ] = None,
    ssl_status: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[list[str] | None, Query()] = None,
    team: Annotated[list[str] | None, Query()] = None,
    application: Annotated[list[str] | None, Query()] = None,
    check_type: Annotated[list[str] | None, Query()] = None,
    protocol: Annotated[list[str] | None, Query()] = None,
    monitoring_enabled: Annotated[bool | None, Query()] = None,
    ssl_expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    sort_by: Annotated[str, Query(description="See /endpoints/filters")] = "name",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    include_uptime: Annotated[
        bool, Query(description="Include 24h uptime per row (one extra query).")
    ] = True,
) -> Page[EndpointListItem]:
    """Paginated endpoint list.

    Combinations work as an AND across dimensions and an OR within one, so
    ``?status=down&environment=<prod>&tag=<backend>`` reads as "down endpoints
    in production tagged backend".
    """
    stmt = endpoint_service.base_query()
    stmt = endpoint_service.apply_search(stmt, search)
    stmt = endpoint_service.apply_filters(
        stmt,
        environment_ids=parse_uuid_list(environment),
        tag_ids=parse_uuid_list(tag),
        statuses=split_csv_param(endpoint_status),
        ssl_statuses=split_csv_param(ssl_status),
        owners=split_csv_param(owner),
        teams=split_csv_param(team),
        applications=split_csv_param(application),
        check_types=split_csv_param(check_type),
        protocols=split_csv_param(protocol),
        monitoring_enabled=monitoring_enabled,
        ssl_expiring_within_days=ssl_expiring_within_days,
    )

    total = await endpoint_service.count_query(session, stmt)
    stmt = endpoint_service.apply_sort(stmt, sort_by, sort_dir)
    rows = list(
        (
            await session.execute(stmt.limit(page.page_size).offset(page.offset))
        )
        .scalars()
        .unique()
        .all()
    )

    endpoint_ids = [row.id for row in rows]
    uptime_map: dict[uuid.UUID, float | None] = {}
    if include_uptime and endpoint_ids:
        uptime_map = await stats_service.uptime_for_endpoints(
            session,
            endpoint_ids,
            since=datetime.now(timezone.utc) - timedelta(hours=24),
        )
    open_incidents = await _open_incident_ids(session, endpoint_ids)

    items = [
        endpoint_to_list_item(
            row,
            uptime_percent=uptime_map.get(row.id),
            has_open_incident=row.id in open_incidents,
        )
        for row in rows
    ]
    return Page.build(items, total=total, page=page.page, page_size=page.page_size)


@router.get(
    "/filters",
    response_model=EndpointFilterOptions,
    summary="Available filter values for the endpoint list",
)
async def filter_options(
    session: DbSession, _user: ReadEndpoints, config: RuntimeConfig
) -> EndpointFilterOptions:
    env_rows = (
        await session.execute(
            select(Environment, func.count(Endpoint.id))
            .outerjoin(Endpoint, Endpoint.environment_id == Environment.id)
            .group_by(Environment.id)
            .order_by(Environment.sort_order, Environment.name)
        )
    ).all()
    tag_rows = (
        await session.execute(
            select(Tag, func.count(Endpoint.id))
            .outerjoin(Tag.endpoints)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()

    environments = []
    for environment, count in env_rows:
        model = EnvironmentRead.model_validate(environment)
        model.endpoint_count = int(count or 0)
        environments.append(model)

    tags = []
    for tag, count in tag_rows:
        model = TagRead.model_validate(tag)
        model.endpoint_count = int(count or 0)
        tags.append(model)

    return EndpointFilterOptions(
        environments=environments,
        tags=tags,
        owners=await endpoint_service.distinct_values(session, "owner"),
        teams=await endpoint_service.distinct_values(session, "team"),
        applications=await endpoint_service.distinct_values(session, "application"),
        allowed_intervals=list(config.get("allowed_intervals", [])),
    )


# ------------------------------------------------------------------ create
@router.post(
    "",
    response_model=EndpointRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an endpoint to monitoring",
)
async def create_endpoint(
    payload: EndpointCreate,
    user: WriteEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> EndpointRead:
    """Create an endpoint.

    The new endpoint is scheduled immediately, so its first real status
    appears within one worker poll rather than after a full interval.
    """
    try:
        endpoint = await endpoint_service.create_endpoint(
            session,
            payload.model_dump(exclude_unset=False),
            config=config,
            created_by_id=user.id,
        )
    except EndpointConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (UrlValidationError, ValueError) as exc:
        raise _bad_request(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_CREATED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=endpoint.name,
        details=endpoint_service.snapshot(endpoint),
        request=request,
    )
    await session.commit()
    await session.refresh(endpoint, ["tags", "environment"])
    return endpoint_to_read(endpoint, created_by=user.username)


# --------------------------------------------------------------- retrieve
@router.get(
    "/{endpoint_id}", response_model=EndpointRead, summary="Endpoint details"
)
async def get_endpoint(
    endpoint_id: uuid.UUID, session: DbSession, _user: ReadEndpoints
) -> EndpointRead:
    endpoint = await _load_endpoint(session, endpoint_id)
    uptime = await stats_service.uptime_for_endpoints(
        session, [endpoint.id], since=datetime.now(timezone.utc) - timedelta(hours=24)
    )
    open_incidents = await _open_incident_ids(session, [endpoint.id])
    return endpoint_to_read(
        endpoint,
        uptime_percent=uptime.get(endpoint.id),
        has_open_incident=endpoint.id in open_incidents,
    )


# ----------------------------------------------------------------- update
@router.put(
    "/{endpoint_id}", response_model=EndpointRead, summary="Update an endpoint"
)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    payload: EndpointUpdate,
    user: WriteEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> EndpointRead:
    endpoint = await _load_endpoint(session, endpoint_id)
    try:
        endpoint, changes = await endpoint_service.update_endpoint(
            session,
            endpoint,
            payload.model_dump(exclude_unset=True),
            config=config,
            updated_by_id=user.id,
        )
    except EndpointConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (UrlValidationError, ValueError) as exc:
        raise _bad_request(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_UPDATED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=endpoint.name,
        details={"changes": changes} if changes else {"changes": "none"},
        request=request,
    )
    await session.commit()
    await session.refresh(endpoint, ["tags", "environment"])
    return endpoint_to_read(endpoint, updated_by=user.username)


@router.patch(
    "/{endpoint_id}/monitoring",
    response_model=EndpointRead,
    summary="Enable, disable, pause or resume monitoring",
)
async def set_monitoring_state(
    endpoint_id: uuid.UUID,
    payload: EndpointStatusUpdate,
    user: WriteEndpoints,
    request: Request,
    session: DbSession,
) -> EndpointRead:
    endpoint = await _load_endpoint(session, endpoint_id)
    before = {
        "monitoring_enabled": endpoint.monitoring_enabled,
        "is_paused": endpoint.is_paused,
    }
    if payload.monitoring_enabled is not None:
        endpoint.monitoring_enabled = payload.monitoring_enabled
    if payload.is_paused is not None:
        endpoint.is_paused = payload.is_paused

    if endpoint.monitoring_enabled and not endpoint.is_paused:
        # Resuming: make it due now and clear any stale lease.
        endpoint.next_check_at = datetime.now(timezone.utc)
        endpoint.lease_expires_at = None
        endpoint.leased_by = None
    else:
        endpoint.next_check_at = None
        # A paused endpoint keeps its last known status but must not be read
        # as healthy or failing while nothing is checking it.
        endpoint.current_status = EndpointStatus.PAUSED.value
        endpoint.consecutive_failures = 0

    endpoint.updated_by_id = user.id
    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_UPDATED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=endpoint.name,
        details={
            "changes": {
                "monitoring_enabled": {
                    "from": before["monitoring_enabled"],
                    "to": endpoint.monitoring_enabled,
                },
                "is_paused": {
                    "from": before["is_paused"],
                    "to": endpoint.is_paused,
                },
            }
        },
        request=request,
    )
    await session.commit()
    await session.refresh(endpoint, ["tags", "environment"])
    return endpoint_to_read(endpoint)


# ----------------------------------------------------------------- delete
@router.delete(
    "/{endpoint_id}",
    response_model=Message,
    summary="Remove an endpoint from monitoring",
)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    user: DeleteEndpoints,
    request: Request,
    session: DbSession,
) -> Message:
    """Delete an endpoint and its monitoring history.

    Results, certificates and incidents cascade with it; the audit entry
    remains as the record that it existed.
    """
    endpoint = await _load_endpoint(session, endpoint_id)
    name, url = endpoint.name, endpoint.url

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_DELETED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=name,
        details={"url": url, "environment": endpoint.environment.name
                 if endpoint.environment else None},
        request=request,
    )
    await session.delete(endpoint)
    await session.commit()
    logger.info("endpoint_deleted", name=name, url=url, by=user.username)
    return Message(detail=f"Endpoint '{name}' deleted.")


# ------------------------------------------------------------- manual check
@router.post(
    "/{endpoint_id}/check",
    response_model=CheckNowResponse,
    summary="Run a check immediately",
)
async def check_endpoint_now(
    endpoint_id: uuid.UUID,
    user: CheckEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
    persist: Annotated[
        bool,
        Query(
            description=(
                "Store the result and apply status/incident transitions. "
                "Set false for a configuration dry run."
            )
        ),
    ] = True,
) -> CheckNowResponse:
    """Probe an endpoint on demand.

    This runs the exact code path the worker uses, so a manual test and a
    scheduled check can never disagree about what "healthy" means.
    """
    endpoint = await _load_endpoint(session, endpoint_id)

    outcome = await monitoring_service.execute_check(endpoint, config)

    incident_opened = incident_closed = None
    previous_status = endpoint.current_status

    if persist:
        recorded = await monitoring_service.record_check_result(
            session,
            endpoint,
            outcome,
            config=config,
            checked_by=user.username,
            is_manual=True,
        )
        incident_opened = (
            recorded.incident_opened.id if recorded.incident_opened else None
        )
        incident_closed = (
            recorded.incident_closed.id if recorded.incident_closed else None
        )
        # Keep the endpoint on its normal cadence rather than counting the
        # manual check as the scheduled one.
        endpoint.next_check_at = monitoring_service.next_check_time(
            endpoint.interval_seconds
        )

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_CHECKED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=endpoint.name,
        details={
            "status": outcome.status,
            "http_status_code": outcome.http_status_code,
            "response_time_ms": outcome.response_time_ms,
            "persisted": persist,
        },
        request=request,
    )
    await session.commit()

    certificate = None
    if outcome.certificate is not None:
        certificate = SslCertificateRead.model_validate(
            {
                **outcome.certificate.to_dict(),
                "endpoint_id": endpoint.id,
                "checked_at": outcome.checked_at,
            }
        )

    return CheckNowResponse(
        endpoint_id=endpoint.id,
        persisted=persist,
        status=outcome.status,
        previous_status=previous_status,
        http_status_code=outcome.http_status_code,
        response_time_ms=outcome.response_time_ms,
        dns_time_ms=outcome.dns_time_ms,
        connect_time_ms=outcome.connect_time_ms,
        tls_time_ms=outcome.tls_time_ms,
        resolved_ip=outcome.resolved_ip,
        error_message=outcome.error_message,
        failure_reason=outcome.failure_reason,
        failure_reason_label=monitoring_service.humanise_reason(
            outcome.failure_reason
        ),
        redirect_count=outcome.redirect_count,
        final_url=outcome.final_url,
        content_length=outcome.content_length,
        checked_at=outcome.checked_at,
        certificate=certificate,
        incident_opened=incident_opened,
        incident_closed=incident_closed,
    )


# ---------------------------------------------------------------- history
@router.post(
    "/{endpoint_id}/diagnose",
    response_model=DiagnosticsResponse,
    summary="Diagnose a failing endpoint",
)
async def diagnose_endpoint(
    endpoint_id: uuid.UUID,
    user: CheckEndpoints,
    request: Request,
    session: DbSession,
) -> DiagnosticsResponse:
    """Isolate which layer of the request is broken, and say what to do.

    Probes DNS, TCP (per resolved address), TLS and HTTP as separate stages,
    then combines that with the endpoint's stored history and with what
    sibling endpoints are doing. The result names the deepest layer that still
    works, so the fault is localised immediately.

    Requires ``endpoint:check`` because it makes live outbound requests. It
    writes nothing to the monitoring history, so diagnosing an endpoint never
    distorts its uptime figures.
    """
    endpoint = await _load_endpoint(session, endpoint_id)
    report = await diagnostics_service.diagnose(session, endpoint)

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINT_CHECKED.value,
        user=user,
        resource_type="endpoint",
        resource_id=endpoint.id,
        resource_name=endpoint.name,
        details={
            "diagnostics": True,
            "verdict": report["verdict"],
            "deepest_layer_ok": report["deepest_layer_ok"],
        },
        request=request,
    )
    await session.commit()
    return DiagnosticsResponse.model_validate(report)


@router.get(
    "/{endpoint_id}/history",
    response_model=Page[MonitoringResultRead],
    summary="Paginated check history",
)
async def endpoint_history(
    endpoint_id: uuid.UUID,
    session: DbSession,
    _user: ReadEndpoints,
    page: Pagination,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    result_status: Annotated[
        list[str] | None, Query(alias="status", description="up, down, degraded")
    ] = None,
    include_headers: Annotated[bool, Query()] = False,
) -> Page[MonitoringResultRead]:
    await _load_endpoint(session, endpoint_id)

    stmt = select(MonitoringResult).where(MonitoringResult.endpoint_id == endpoint_id)
    if since is not None:
        stmt = stmt.where(MonitoringResult.checked_at >= since)
    if until is not None:
        stmt = stmt.where(MonitoringResult.checked_at <= until)
    statuses = split_csv_param(result_status)
    if statuses:
        stmt = stmt.where(MonitoringResult.status.in_(statuses))

    count_stmt = stmt.with_only_columns(func.count(MonitoringResult.id)).order_by(None)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    rows = list(
        (
            await session.execute(
                stmt.order_by(MonitoringResult.checked_at.desc())
                .limit(page.page_size)
                .offset(page.offset)
            )
        )
        .scalars()
        .all()
    )

    items: list[MonitoringResultRead] = []
    for row in rows:
        model = MonitoringResultRead.model_validate(row)
        model.failure_reason_label = monitoring_service.humanise_reason(
            row.failure_reason
        )
        if not include_headers:
            model.response_headers = None
        items.append(model)

    return Page.build(items, total=total, page=page.page, page_size=page.page_size)


@router.get(
    "/{endpoint_id}/stats",
    response_model=EndpointStatsResponse,
    summary="Availability windows and the response-time series",
)
async def endpoint_stats(
    endpoint_id: uuid.UUID,
    session: DbSession,
    _user: ReadEndpoints,
    window: Annotated[
        str, Query(pattern="^(24h|7d|30d|90d)$", description="Series span.")
    ] = "24h",
) -> EndpointStatsResponse:
    """The availability block and graph data for the detail page."""
    await _load_endpoint(session, endpoint_id)

    summary = await stats_service.availability_summary(session, endpoint_id)
    until = datetime.now(timezone.utc)
    since = until - stats_service.WINDOWS[window]
    bucket_seconds = stats_service.choose_bucket_seconds(until - since)
    series = await stats_service.response_time_series(
        session,
        endpoint_id,
        since=since,
        until=until,
        bucket_seconds=bucket_seconds,
    )

    return EndpointStatsResponse(
        endpoint_id=endpoint_id,
        generated_at=until,
        windows={
            name: WindowStats.model_validate(values)
            for name, values in summary.items()
        },
        series=[TimeSeriesPoint.model_validate(point) for point in series],
        bucket_seconds=bucket_seconds,
    )


@router.get(
    "/{endpoint_id}/ssl",
    response_model=SslCertificateRead,
    summary="Current SSL certificate for an endpoint",
)
async def endpoint_ssl(
    endpoint_id: uuid.UUID, session: DbSession, _user: ReadEndpoints
) -> SslCertificateRead:
    endpoint = await _load_endpoint(session, endpoint_id)
    if not endpoint.ssl_monitoring_enabled or endpoint.protocol != "https":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSL monitoring is not enabled for this endpoint.",
        )

    certificate = (
        await session.execute(
            select(SslCertificate)
            .where(
                SslCertificate.endpoint_id == endpoint_id,
                SslCertificate.is_current.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if certificate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No certificate has been captured yet. "
                f"Current SSL state: {endpoint.ssl_status}."
            ),
        )
    return SslCertificateRead.model_validate(certificate)


@router.get(
    "/{endpoint_id}/ssl/history",
    response_model=list[SslCertificateRead],
    summary="Certificate history for an endpoint",
)
async def endpoint_ssl_history(
    endpoint_id: uuid.UUID,
    session: DbSession,
    _user: ReadEndpoints,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SslCertificateRead]:
    """Past certificates, newest first - useful for auditing renewals."""
    await _load_endpoint(session, endpoint_id)
    rows = (
        await session.execute(
            select(SslCertificate)
            .where(SslCertificate.endpoint_id == endpoint_id)
            .order_by(SslCertificate.first_seen_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [SslCertificateRead.model_validate(row) for row in rows]


# ------------------------------------------------------------ bulk actions
@router.post(
    "/bulk",
    response_model=BulkActionResult,
    summary="Apply an action to many endpoints at once",
)
async def bulk_action(
    payload: BulkEndpointAction,
    user: WriteEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> BulkActionResult:
    """Enable, disable, pause, resume, delete, re-check or (un)tag in bulk.

    ``delete`` additionally requires the endpoint:delete permission, checked
    here rather than by the route dependency so the other actions stay
    available to any endpoint editor.
    """
    from app.core.enums import Permission as PermissionCode

    if payload.action == "delete" and not user.has_permission(
        PermissionCode.ENDPOINT_DELETE.value
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit deleting endpoints.",
        )
    if payload.action == "check" and not user.has_permission(
        PermissionCode.ENDPOINT_CHECK.value
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit running checks.",
        )

    rows = list(
        (
            await session.execute(
                endpoint_service.base_query().where(
                    Endpoint.id.in_(payload.endpoint_ids)
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    found_ids = {row.id for row in rows}
    errors: list[dict[str, Any]] = [
        {"endpoint_id": str(missing), "error": "not found"}
        for missing in payload.endpoint_ids
        if missing not in found_ids
    ]

    succeeded = 0
    now = datetime.now(timezone.utc)
    tags = (
        await endpoint_service.resolve_tags(session, payload.tags)
        if payload.action in ("tag", "untag")
        else []
    )

    for endpoint in rows:
        try:
            if payload.action == "enable":
                endpoint.monitoring_enabled = True
                endpoint.is_paused = False
                endpoint.next_check_at = now
            elif payload.action == "disable":
                endpoint.monitoring_enabled = False
                endpoint.next_check_at = None
                endpoint.current_status = EndpointStatus.PAUSED.value
            elif payload.action == "pause":
                endpoint.is_paused = True
                endpoint.next_check_at = None
                endpoint.current_status = EndpointStatus.PAUSED.value
            elif payload.action == "resume":
                endpoint.is_paused = False
                endpoint.monitoring_enabled = True
                endpoint.next_check_at = now
            elif payload.action == "delete":
                await session.delete(endpoint)
            elif payload.action == "check":
                # Due immediately; the worker picks it up on its next poll
                # instead of the API running hundreds of probes inline.
                endpoint.next_check_at = now
                endpoint.lease_expires_at = None
                endpoint.leased_by = None
            elif payload.action == "tag":
                existing = {t.id for t in endpoint.tags}
                for tag in tags:
                    if tag.id not in existing:
                        endpoint.tags.append(tag)
            elif payload.action == "untag":
                remove = {t.id for t in tags}
                endpoint.tags = [t for t in endpoint.tags if t.id not in remove]

            if payload.action != "delete":
                endpoint.updated_by_id = user.id
            succeeded += 1
        except Exception as exc:  # pragma: no cover - defensive
            errors.append({"endpoint_id": str(endpoint.id), "error": str(exc)})

    await audit_service.record(
        session,
        action=(
            AuditAction.ENDPOINT_DELETED.value
            if payload.action == "delete"
            else AuditAction.ENDPOINT_UPDATED.value
        ),
        user=user,
        resource_type="endpoint",
        details={
            "bulk_action": payload.action,
            "requested": len(payload.endpoint_ids),
            "succeeded": succeeded,
            "tags": payload.tags,
        },
        request=request,
    )
    await session.commit()

    return BulkActionResult(
        requested=len(payload.endpoint_ids),
        succeeded=succeeded,
        failed=len(errors),
        errors=errors,
    )
