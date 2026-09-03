"""Dashboard aggregation and the SSL certificate dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import (
    DbSession,
    Pagination,
    ReadEndpoints,
    RuntimeConfig,
    parse_uuid_list,
    split_csv_param,
)
from app.core.enums import SslStatus
from app.models.endpoint import Endpoint
from app.models.monitoring import SslCertificate
from app.schemas.common import Page
from app.schemas.dashboard import (
    DashboardResponse,
    ExpiryBucket,
    GroupAvailability,
    SummaryCards,
    TimeSeriesPoint,
)
from app.schemas.monitoring import (
    IncidentRead,
    SslDashboardRow,
    SslSummary,
    incident_to_schema,
)
from app.services import monitoring_service, stats_service
from app.services.stats_service import DashboardFilters

router = APIRouter(tags=["Dashboard"])


def _build_filters(
    environment: list[str] | None,
    tag: list[str] | None,
    owner: list[str] | None,
    team: list[str] | None,
    application: list[str] | None,
    endpoint_status: list[str] | None,
) -> DashboardFilters:
    return DashboardFilters(
        environment_ids=parse_uuid_list(environment),
        tag_ids=parse_uuid_list(tag),
        owners=split_csv_param(owner),
        teams=split_csv_param(team),
        applications=split_csv_param(application),
        statuses=split_csv_param(endpoint_status),
    )


def _incident_to_schema(incident) -> IncidentRead:
    return incident_to_schema(
        incident,
        reason_label=monitoring_service.humanise_reason(incident.reason),
    )


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Everything the dashboard renders, in one request",
)
async def get_dashboard(
    session: DbSession,
    _user: ReadEndpoints,
    config: RuntimeConfig,
    window: Annotated[str, Query(pattern="^(24h|7d|30d|90d)$")] = "24h",
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[list[str] | None, Query()] = None,
    team: Annotated[list[str] | None, Query()] = None,
    application: Annotated[list[str] | None, Query()] = None,
    endpoint_status: Annotated[list[str] | None, Query(alias="status")] = None,
) -> DashboardResponse:
    """Aggregate the dashboard in a single round trip.

    Every figure is derived from stored check results, so the cards, charts and
    incident list are guaranteed to describe the same moment in time.
    """
    filters = _build_filters(
        environment, tag, owner, team, application, endpoint_status
    )

    until = datetime.now(timezone.utc)
    since = until - stats_service.WINDOWS[window]

    summary = await stats_service.dashboard_summary(
        session, filters=filters, window=window
    )
    endpoint_ids = await stats_service.filtered_endpoint_ids(session, filters)

    series = await stats_service.global_response_time_series(
        session, since=since, until=until, endpoint_ids=endpoint_ids
    )

    by_environment = await stats_service.availability_by_group(
        session, group="environment", filters=filters, window=window
    )
    by_tag = await stats_service.availability_by_group(
        session, group="tag", filters=filters, window=window
    )
    by_team = await stats_service.availability_by_group(
        session, group="team", filters=filters, window=window
    )

    timeline = await stats_service.ssl_expiry_timeline(session, filters=filters)
    failing = await stats_service.failure_counts(
        session, since=since, until=until, filters=filters
    )
    slowest = await stats_service.slowest_endpoints(
        session, since=since, until=until, filters=filters
    )

    open_incidents = await stats_service.recent_incidents(
        session, limit=25, filters=filters, open_only=True
    )
    recent = await stats_service.recent_incidents(
        session, limit=15, filters=filters, open_only=False
    )

    sla_target = float(config.get("uptime_sla_target", 99.9))
    breaches = stats_service.sla_breaches(by_environment, target=sla_target)

    return DashboardResponse(
        generated_at=until,
        summary=SummaryCards.model_validate(summary),
        response_time_series=[TimeSeriesPoint.model_validate(p) for p in series],
        availability_by_environment=[
            GroupAvailability.model_validate(g) for g in by_environment
        ],
        availability_by_tag=[GroupAvailability.model_validate(g) for g in by_tag],
        availability_by_team=[GroupAvailability.model_validate(g) for g in by_team],
        ssl_expiry_timeline=[ExpiryBucket.model_validate(b) for b in timeline],
        top_failing_endpoints=failing,  # type: ignore[arg-type]
        slowest_endpoints=slowest,  # type: ignore[arg-type]
        open_incidents=[_incident_to_schema(i) for i in open_incidents],
        recent_incidents=[_incident_to_schema(i) for i in recent],
        sla_target=sla_target,
        sla_breaches=[GroupAvailability.model_validate(g) for g in breaches],
    )


@router.get(
    "/dashboard/summary",
    response_model=SummaryCards,
    summary="Just the summary cards (cheap enough to poll)",
)
async def get_summary(
    session: DbSession,
    _user: ReadEndpoints,
    window: Annotated[str, Query(pattern="^(24h|7d|30d|90d)$")] = "24h",
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[list[str] | None, Query()] = None,
    team: Annotated[list[str] | None, Query()] = None,
    application: Annotated[list[str] | None, Query()] = None,
    endpoint_status: Annotated[list[str] | None, Query(alias="status")] = None,
) -> SummaryCards:
    filters = _build_filters(
        environment, tag, owner, team, application, endpoint_status
    )
    summary = await stats_service.dashboard_summary(
        session, filters=filters, window=window
    )
    return SummaryCards.model_validate(summary)


@router.get(
    "/dashboard/availability",
    response_model=list[GroupAvailability],
    summary="Availability grouped by environment, tag, team, owner or application",
)
async def get_availability_by_group(
    session: DbSession,
    _user: ReadEndpoints,
    group: Annotated[
        str, Query(pattern="^(environment|tag|team|owner|application)$")
    ] = "environment",
    window: Annotated[str, Query(pattern="^(24h|7d|30d|90d)$")] = "24h",
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
) -> list[GroupAvailability]:
    filters = _build_filters(environment, tag, None, None, None, None)
    groups = await stats_service.availability_by_group(
        session, group=group, filters=filters, window=window
    )
    return [GroupAvailability.model_validate(g) for g in groups]


# ------------------------------------------------------------ SSL dashboard
_SSL_SORT_COLUMNS = {
    "expiry": SslCertificate.valid_to,
    "expires_at": SslCertificate.valid_to,
    "remaining": SslCertificate.days_remaining,
    "days_remaining": SslCertificate.days_remaining,
    "status": SslCertificate.status,
    "issuer": SslCertificate.issuer_common_name,
    "common_name": SslCertificate.common_name,
    "endpoint": Endpoint.name,
    "environment": Endpoint.environment_id,
    "checked_at": SslCertificate.checked_at,
}


@router.get(
    "/ssl/summary",
    response_model=SslSummary,
    summary="Certificate state counts",
)
async def ssl_summary(
    session: DbSession, _user: ReadEndpoints, config: RuntimeConfig
) -> SslSummary:
    counts = await stats_service.certificate_summary(session)
    self_signed = int(
        (
            await session.execute(
                select(func.count(SslCertificate.id)).where(
                    SslCertificate.is_current.is_(True),
                    SslCertificate.is_self_signed.is_(True),
                )
            )
        ).scalar()
        or 0
    )
    return SslSummary(
        total=sum(counts.values()),
        valid=counts.get(SslStatus.VALID.value, 0),
        expiring_soon=counts.get(SslStatus.EXPIRING_SOON.value, 0),
        critical=counts.get(SslStatus.CRITICAL.value, 0),
        expired=counts.get(SslStatus.EXPIRED.value, 0),
        invalid=counts.get(SslStatus.INVALID.value, 0),
        unable_to_check=counts.get(SslStatus.UNABLE_TO_CHECK.value, 0),
        self_signed=self_signed,
        warning_days=int(config.get("ssl_warning_days", 30)),
        critical_days=int(config.get("ssl_critical_days", 7)),
    )


@router.get(
    "/ssl",
    response_model=Page[SslDashboardRow],
    summary="SSL certificate table with sorting and filtering",
)
async def list_certificates(
    session: DbSession,
    _user: ReadEndpoints,
    page: Pagination,
    search: Annotated[str | None, Query()] = None,
    cert_status: Annotated[list[str] | None, Query(alias="status")] = None,
    issuer: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    self_signed: Annotated[bool | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "remaining",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
) -> Page[SslDashboardRow]:
    """The dedicated SSL monitoring page.

    Joins the current certificate observation to its endpoint so a single
    query can sort by expiry while still filtering on environment and tags.
    """
    from app.models.endpoint import endpoint_tags

    stmt = (
        select(SslCertificate, Endpoint)
        .join(Endpoint, Endpoint.id == SslCertificate.endpoint_id)
        .where(SslCertificate.is_current.is_(True))
    )

    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Endpoint.name).like(needle),
                func.lower(Endpoint.url).like(needle),
                func.lower(Endpoint.hostname).like(needle),
                func.lower(func.coalesce(SslCertificate.common_name, "")).like(needle),
                func.lower(func.coalesce(SslCertificate.issuer, "")).like(needle),
            )
        )

    statuses = split_csv_param(cert_status)
    if statuses:
        stmt = stmt.where(SslCertificate.status.in_(statuses))

    issuers = split_csv_param(issuer)
    if issuers:
        stmt = stmt.where(
            or_(
                SslCertificate.issuer_common_name.in_(issuers),
                SslCertificate.issuer_organization.in_(issuers),
            )
        )

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

    if expiring_within_days is not None:
        stmt = stmt.where(
            SslCertificate.days_remaining.isnot(None),
            SslCertificate.days_remaining <= expiring_within_days,
        )
    if self_signed is not None:
        stmt = stmt.where(SslCertificate.is_self_signed.is_(self_signed))

    count_stmt = stmt.with_only_columns(func.count(SslCertificate.id)).order_by(None)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    column = _SSL_SORT_COLUMNS.get(sort_by.lower(), SslCertificate.days_remaining)
    ordering = column.desc() if sort_dir == "desc" else column.asc()
    stmt = stmt.order_by(ordering.nulls_last(), Endpoint.name.asc())

    rows = (
        await session.execute(stmt.limit(page.page_size).offset(page.offset))
    ).all()

    items = [
        SslDashboardRow(
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.name,
            url=endpoint.url,
            hostname=endpoint.hostname,
            environment=endpoint.environment.name if endpoint.environment else None,
            tags=endpoint.tag_names,
            owner=endpoint.owner,
            common_name=certificate.common_name,
            issuer=certificate.issuer_common_name or certificate.issuer,
            issuer_organization=certificate.issuer_organization,
            valid_from=certificate.valid_from,
            expires_at=certificate.valid_to,
            days_remaining=certificate.days_remaining,
            status=certificate.status,
            is_self_signed=certificate.is_self_signed,
            is_wildcard=certificate.is_wildcard,
            tls_version=certificate.tls_version,
            key_algorithm=certificate.key_algorithm,
            key_size=certificate.key_size,
            signature_algorithm=certificate.signature_algorithm,
            hostname_matches=certificate.hostname_matches,
            chain_verified=certificate.chain_verified,
            verification_status=certificate.verification_status,
            san_count=len(certificate.san or []),
            checked_at=certificate.checked_at,
        )
        for certificate, endpoint in rows
    ]
    return Page.build(items, total=total, page=page.page, page_size=page.page_size)


@router.get(
    "/ssl/issuers",
    response_model=list[str],
    summary="Distinct certificate issuers, for the issuer filter",
)
async def ssl_issuers(session: DbSession, _user: ReadEndpoints) -> list[str]:
    rows = (
        await session.execute(
            select(SslCertificate.issuer_common_name)
            .where(
                SslCertificate.is_current.is_(True),
                SslCertificate.issuer_common_name.isnot(None),
            )
            .distinct()
            .order_by(SslCertificate.issuer_common_name)
        )
    ).scalars().all()
    return [row for row in rows if row]
