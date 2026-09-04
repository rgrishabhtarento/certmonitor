"""RCA management, and the locally computed operational intelligence.

Permissions here introduce no new roles, which was a hard requirement.
Viewing follows ``incident:read``; requesting, assigning and completing follow
``incident:write`` **or** ownership of the RCA itself. That last clause is what
lets an RCA be assigned to a viewer, or to a team a viewer belongs to, and have
them actually able to complete it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import (
    DbSession,
    Pagination,
    parse_uuid_list,
    require_permissions,
    split_csv_param,
)
from app.core.enums import AuditAction, Permission, RcaStatus
from app.core.logging import get_logger
from app.models.endpoint import Endpoint
from app.models.incident import Incident
from app.models.rca import Rca
from app.models.user import User
from app.schemas.common import Page
from app.schemas.insights import DailySummary, SearchResult, SmartSummary
from app.schemas.rca import (
    CommentCreate,
    IncidentCommentRead,
    NotRequiredRequest,
    RcaAnalytics,
    RcaAssign,
    RcaDashboard,
    RcaDraft,
    RcaListItem,
    RcaOptions,
    RcaRead,
    RcaRequest,
    RcaUpdate,
    to_list_item,
    to_read,
)
from app.services import audit_service, insights_service, rca_service
from app.services.rca_service import RcaError

logger = get_logger(__name__)

router = APIRouter(tags=["RCA & Intelligence"])

ReadIncidents = Annotated[User, Depends(require_permissions(Permission.INCIDENT_READ))]
WriteIncidents = Annotated[User, Depends(require_permissions(Permission.INCIDENT_WRITE))]


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _load_rca(session, rca_id: int) -> Rca:
    rca = (
        await session.execute(select(Rca).where(Rca.id == rca_id))
    ).scalars().first()
    if rca is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="RCA not found."
        )
    return rca


async def _load_incident(session, incident_id: int) -> Incident:
    incident = (
        await session.execute(select(Incident).where(Incident.id == incident_id))
    ).scalars().unique().first()
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found."
        )
    return incident


def _permissions(rca: Rca, user: User) -> dict[str, bool]:
    editable = rca_service.can_edit(rca, user)
    return {
        "can_edit": editable and rca.status != RcaStatus.COMPLETED.value,
        "can_assign": rca_service.can_assign(user),
        "can_complete": (
            editable
            and rca.status
            in (RcaStatus.PENDING.value, RcaStatus.IN_PROGRESS.value)
        ),
    }


def _incident_summary(incident: Incident | None) -> dict[str, Any] | None:
    if incident is None:
        return None
    return {
        "id": incident.id,
        "status": incident.status,
        "severity": incident.severity,
        "started_at": incident.started_at,
        "resolved_at": incident.resolved_at,
        "duration_seconds": incident.duration_seconds,
        "reason": incident.reason,
        "error_message": incident.error_message,
        "failed_check_count": incident.failed_check_count,
        "endpoint_id": str(incident.endpoint_id),
    }


async def _require_editable(session, rca: Rca, user: User) -> None:
    if not rca_service.can_edit(rca, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You can edit this RCA only if you own it, or hold "
                "incident:write."
            ),
        )


# ============================================================ intelligence
@router.get(
    "/intelligence/summary",
    response_model=SmartSummary,
    summary="Smart DevOps summary - what needs attention now",
)
async def smart_summary(
    session: DbSession, _user: ReadIncidents
) -> SmartSummary:
    """Computed entirely from local rows. Nothing leaves this server."""
    return SmartSummary.model_validate(await insights_service.smart_summary(session))


@router.get(
    "/intelligence/daily",
    response_model=DailySummary,
    summary="Daily operations summary",
)
async def daily_summary(
    session: DbSession,
    _user: ReadIncidents,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> DailySummary:
    return DailySummary.model_validate(
        await insights_service.daily_summary(session, hours=hours)
    )


@router.get(
    "/intelligence/search",
    response_model=SearchResult,
    summary="Search the infrastructure in plain language",
)
async def infrastructure_search(
    session: DbSession,
    _user: ReadIncidents,
    q: Annotated[str, Query(max_length=300, description="e.g. 'production services that are down'")],
) -> SearchResult:
    """A deterministic local parser over a fixed vocabulary.

    The question is matched against known intents and executed as a normal
    database query. Nothing is sent anywhere, the same question always
    returns the same rows, and an unrecognised question says so rather than
    guessing - see ``understood`` in the response.
    """
    return SearchResult.model_validate(await insights_service.search(session, q))


# ==================================================================== RCA
@router.get(
    "/rca/dashboard", response_model=RcaDashboard, summary="RCA overview"
)
async def rca_dashboard(session: DbSession, _user: ReadIncidents) -> RcaDashboard:
    payload = await rca_service.dashboard(session)
    payload["pending_queue"] = [
        to_list_item(row) for row in payload["pending_queue"]
    ]
    return RcaDashboard.model_validate(payload)


@router.get(
    "/rca/analytics", response_model=RcaAnalytics, summary="RCA reporting"
)
async def rca_analytics(
    session: DbSession,
    _user: ReadIncidents,
    window_days: Annotated[int, Query(ge=7, le=365)] = 90,
) -> RcaAnalytics:
    """Built only from stored RCA records.

    An empty section means nobody has recorded that yet - never that nothing
    happened.
    """
    payload = await rca_service.analytics(session, window_days=window_days)
    payload["recurring_root_causes"] = await rca_service.recurring_root_causes(
        session, window_days=window_days
    )
    return RcaAnalytics.model_validate(payload)


@router.get("/rca/options", response_model=RcaOptions, summary="Filter options")
async def rca_options(session: DbSession, _user: ReadIncidents) -> RcaOptions:
    teams = [
        row
        for row in (
            await session.execute(
                select(User.team).where(User.team.is_not(None)).distinct()
            )
        ).scalars()
        if row
    ]
    applications = [
        row
        for row in (
            await session.execute(
                select(Rca.application).where(Rca.application.is_not(None)).distinct()
            )
        ).scalars()
        if row
    ]
    return RcaOptions(teams=sorted(teams), applications=sorted(applications))


@router.get("/rca", response_model=Page[RcaListItem], summary="List RCAs")
async def list_rcas(
    session: DbSession,
    user: ReadIncidents,
    page: Pagination,
    search: Annotated[str | None, Query()] = None,
    rca_status: Annotated[list[str] | None, Query(alias="status")] = None,
    application: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    owner_team: Annotated[str | None, Query()] = None,
    owner_user_id: Annotated[uuid.UUID | None, Query()] = None,
    mine: Annotated[bool, Query(description="Assigned to me or to my team.")] = False,
    overdue: Annotated[bool, Query()] = False,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> Page[RcaListItem]:
    stmt = rca_service.apply_filters(
        rca_service.base_query(),
        search=search,
        statuses=split_csv_param(rca_status),
        applications=split_csv_param(application),
        environments=split_csv_param(environment),
        categories=split_csv_param(category),
        owner_user_id=owner_user_id,
        owner_team=owner_team,
        mine_for=user if mine else None,
        overdue_only=overdue,
        since=since,
        until=until,
    )
    total = await rca_service.count_query(session, stmt)
    rows = list(
        (
            await session.execute(
                stmt.order_by(Rca.created_at.desc())
                .limit(page.page_size)
                .offset(page.offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return Page.build(
        [to_list_item(row) for row in rows],
        total=total, page=page.page, page_size=page.page_size,
    )


@router.get("/rca/{rca_id}", response_model=RcaRead, summary="RCA details")
async def get_rca(
    rca_id: int, session: DbSession, user: ReadIncidents
) -> RcaRead:
    rca = await _load_rca(session, rca_id)
    incident = (
        await session.execute(select(Incident).where(Incident.id == rca.incident_id))
    ).scalars().unique().first()

    return to_read(
        rca,
        permissions=_permissions(rca, user),
        incident=_incident_summary(incident),
        comments=await rca_service.comments_for(session, rca.incident_id),
        similar_past=(
            await rca_service.similar_past_rcas(session, incident)
            if incident is not None
            else []
        ),
    )


# ------------------------------------------------- from the incident page
@router.get(
    "/incidents/{incident_id}/rca",
    response_model=RcaRead | None,
    summary="The RCA for an incident, if one exists",
)
async def rca_for_incident(
    incident_id: int, session: DbSession, user: ReadIncidents
) -> RcaRead | None:
    """Returns null when no RCA has been requested.

    Null is the normal state, not an error - RCA is optional, and most
    incidents will never have one.
    """
    incident = await _load_incident(session, incident_id)
    rca = await rca_service.get_for_incident(session, incident_id)
    if rca is None:
        return None
    return to_read(
        rca,
        permissions=_permissions(rca, user),
        incident=_incident_summary(incident),
        comments=await rca_service.comments_for(session, incident_id),
        similar_past=await rca_service.similar_past_rcas(session, incident),
    )


@router.post(
    "/incidents/{incident_id}/rca",
    response_model=RcaRead,
    status_code=status.HTTP_201_CREATED,
    summary="Request an RCA for an incident",
)
async def request_rca(
    incident_id: int,
    payload: RcaRequest,
    user: WriteIncidents,
    request: Request,
    session: DbSession,
) -> RcaRead:
    """Open an RCA. This never changes the incident.

    Idempotent - requesting twice returns the existing RCA rather than
    creating a second one.
    """
    incident = await _load_incident(session, incident_id)

    owner_user = None
    if payload.owner_user_id:
        owner_user = (
            await session.execute(select(User).where(User.id == payload.owner_user_id))
        ).scalars().first()
        if owner_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found."
            )

    try:
        rca = await rca_service.request_rca(
            session, incident, user=user,
            owner_type=payload.owner_type,
            owner_user=owner_user,
            owner_team=payload.owner_team,
            due_in_days=payload.due_in_days,
        )
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.RCA_REQUESTED.value,
        user=user, resource_type="rca", resource_id=rca.id,
        resource_name=f"RCA for incident {incident_id}",
        details={"owner": rca.owner_label, "incident_id": incident_id},
        request=request,
    )
    await session.commit()

    reloaded = await _load_rca(session, rca.id)
    return to_read(
        reloaded,
        permissions=_permissions(reloaded, user),
        incident=_incident_summary(incident),
        comments=await rca_service.comments_for(session, incident_id),
        similar_past=await rca_service.similar_past_rcas(session, incident),
    )


@router.post(
    "/incidents/{incident_id}/rca/not-required",
    response_model=RcaRead,
    summary="Record that this incident does not need an RCA",
)
async def rca_not_required(
    incident_id: int,
    payload: NotRequiredRequest,
    user: WriteIncidents,
    request: Request,
    session: DbSession,
) -> RcaRead:
    """A deliberate decision, worth storing.

    "We looked and decided not to" is a different state from "nobody has
    looked", and only the first should leave the pending queue.
    """
    incident = await _load_incident(session, incident_id)
    try:
        rca = await rca_service.mark_not_required(
            session, incident, user=user, reason=payload.reason
        )
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.RCA_NOT_REQUIRED.value,
        user=user, resource_type="rca", resource_id=rca.id,
        resource_name=f"RCA for incident {incident_id}",
        details={"reason": payload.reason}, request=request,
    )
    await session.commit()

    reloaded = await _load_rca(session, rca.id)
    return to_read(
        reloaded,
        permissions=_permissions(reloaded, user),
        incident=_incident_summary(incident),
    )


# ------------------------------------------------------------- comments
@router.get(
    "/incidents/{incident_id}/comments",
    response_model=list[IncidentCommentRead],
    summary="Incident comments",
)
async def list_incident_comments(
    incident_id: int, session: DbSession, _user: ReadIncidents
) -> list[IncidentCommentRead]:
    await _load_incident(session, incident_id)
    return [
        IncidentCommentRead(
            id=c.id, username=c.username, body=c.body, created_at=c.created_at
        )
        for c in await rca_service.comments_for(session, incident_id)
    ]


@router.post(
    "/incidents/{incident_id}/comments",
    response_model=IncidentCommentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on an incident",
)
async def add_incident_comment(
    incident_id: int,
    payload: CommentCreate,
    user: ReadIncidents,
    request: Request,
    session: DbSession,
) -> IncidentCommentRead:
    """Anyone who can see the incident can comment.

    The investigation happens in conversation, and that conversation is the
    raw material of the RCA - so it belongs on the incident rather than in
    chat, where the RCA owner would have to reconstruct it.
    """
    incident = await _load_incident(session, incident_id)
    try:
        comment = await rca_service.add_comment(
            session, incident, user=user, body=payload.body
        )
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_COMMENTED.value,
        user=user, resource_type="incident", resource_id=incident.id,
        resource_name=f"INC-{incident.id}", request=request,
    )
    await session.commit()
    return IncidentCommentRead(
        id=comment.id, username=comment.username,
        body=comment.body, created_at=comment.created_at,
    )


# ------------------------------------------------------------- workflow
@router.put("/rca/{rca_id}", response_model=RcaRead, summary="Save RCA content")
async def update_rca(
    rca_id: int,
    payload: RcaUpdate,
    user: ReadIncidents,
    request: Request,
    session: DbSession,
) -> RcaRead:
    """Save partial work. This does not complete the RCA."""
    rca = await _load_rca(session, rca_id)
    await _require_editable(session, rca, user)

    body = payload.model_dump(exclude_unset=True)
    if "preventive_actions" in body and body["preventive_actions"] is not None:
        body["preventive_actions"] = [
            item if isinstance(item, dict) else item.model_dump()
            for item in body["preventive_actions"]
        ]
    if "timeline" in body and body["timeline"] is not None:
        body["timeline"] = [
            {
                **(item if isinstance(item, dict) else item.model_dump()),
                "at": (
                    (item.get("at") if isinstance(item, dict) else item.at).isoformat()
                    if (item.get("at") if isinstance(item, dict) else item.at)
                    else None
                ),
            }
            for item in body["timeline"]
        ]

    try:
        await rca_service.save(session, rca, body, user=user)
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.RCA_UPDATED.value,
        user=user, resource_type="rca", resource_id=rca.id,
        resource_name=f"RCA {rca.id}", request=request,
    )
    await session.commit()

    reloaded = await _load_rca(session, rca_id)
    incident = (
        await session.execute(select(Incident).where(Incident.id == reloaded.incident_id))
    ).scalars().unique().first()
    return to_read(
        reloaded,
        permissions=_permissions(reloaded, user),
        incident=_incident_summary(incident),
        comments=await rca_service.comments_for(session, reloaded.incident_id),
    )


@router.post("/rca/{rca_id}/assign", response_model=RcaRead, summary="Assign an RCA")
async def assign_rca(
    rca_id: int,
    payload: RcaAssign,
    user: WriteIncidents,
    request: Request,
    session: DbSession,
) -> RcaRead:
    rca = await _load_rca(session, rca_id)

    owner_user = None
    if payload.owner_user_id:
        owner_user = (
            await session.execute(select(User).where(User.id == payload.owner_user_id))
        ).scalars().first()
        if owner_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found."
            )

    try:
        await rca_service.assign(
            session, rca, user=user,
            owner_type=payload.owner_type,
            owner_user=owner_user,
            owner_team=payload.owner_team,
            due_in_days=payload.due_in_days,
        )
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.RCA_ASSIGNED.value,
        user=user, resource_type="rca", resource_id=rca.id,
        resource_name=f"RCA {rca.id}",
        details={"owner": rca.owner_label}, request=request,
    )
    await session.commit()

    reloaded = await _load_rca(session, rca_id)
    return to_read(reloaded, permissions=_permissions(reloaded, user))


@router.post(
    "/rca/{rca_id}/draft",
    response_model=RcaDraft,
    summary="Generate an RCA draft from local data",
)
async def generate_rca_draft(
    rca_id: int, session: DbSession, user: ReadIncidents
) -> RcaDraft:
    """Assemble a starting point from records this server already holds.

    Not AI-generated and not called that: it is a template filled from the
    incident, the diagnosis run at the time, the deployment that preceded it,
    monitoring history and the comments. Where the data does not support a
    statement it says so. The owner reviews and edits before saving.
    """
    rca = await _load_rca(session, rca_id)
    incident = await _load_incident(session, rca.incident_id)
    draft = await rca_service.generate_draft(session, rca, incident)
    return RcaDraft.model_validate(draft)


@router.post(
    "/rca/{rca_id}/complete", response_model=RcaRead, summary="Complete an RCA"
)
async def complete_rca(
    rca_id: int,
    user: ReadIncidents,
    request: Request,
    session: DbSession,
) -> RcaRead:
    """Mark the analysis done.

    Deliberately does not touch the incident - the two lifecycles are
    independent, and an incident closed with a pending RCA is a valid and
    common state.
    """
    rca = await _load_rca(session, rca_id)
    await _require_editable(session, rca, user)

    try:
        await rca_service.complete(session, rca, user=user)
    except RcaError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.RCA_COMPLETED.value,
        user=user, resource_type="rca", resource_id=rca.id,
        resource_name=f"RCA {rca.id}",
        details={
            "category": rca.root_cause_category,
            "incident_id": rca.incident_id,
        },
        request=request,
    )
    await session.commit()

    reloaded = await _load_rca(session, rca_id)
    incident = (
        await session.execute(select(Incident).where(Incident.id == reloaded.incident_id))
    ).scalars().unique().first()
    return to_read(
        reloaded,
        permissions=_permissions(reloaded, user),
        incident=_incident_summary(incident),
        comments=await rca_service.comments_for(session, reloaded.incident_id),
    )
