"""Change management: request, approve, deploy, complete."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    DbSession,
    Pagination,
    RuntimeConfig,
    parse_uuid_list,
    require_permissions,
    split_csv_param,
)
from app.core.enums import AuditAction, ChangeStatus, Permission, RoleName
from app.core.logging import get_logger
from app.models.change import Change
from app.models.user import User
from app.schemas.change import (
    ApprovalRequest,
    CancelRequest,
    ChangeCreate,
    ChangeDashboard,
    ChangeListItem,
    ChangeOptions,
    ChangeRead,
    ChangeUpdate,
    CommentCreate,
    CompleteDeploymentRequest,
    DeploymentResult,
    FailDeploymentRequest,
    RejectionRequest,
    to_list_item,
    to_read,
)
from app.schemas.common import Page
from app.services import audit_service, change_service
from app.services.change_service import ChangeError, DuplicateDeployment

logger = get_logger(__name__)

router = APIRouter(prefix="/changes", tags=["Change Management"])

# Permission gates, named for readability at the call sites.
CanRead = Annotated[User, Depends(require_permissions(Permission.CHANGE_READ))]
CanWrite = Annotated[User, Depends(require_permissions(Permission.CHANGE_WRITE))]
CanApprove = Annotated[User, Depends(require_permissions(Permission.CHANGE_APPROVE))]
CanDeploy = Annotated[User, Depends(require_permissions(Permission.CHANGE_DEPLOY))]
CanComment = Annotated[User, Depends(require_permissions(Permission.CHANGE_COMMENT))]


# ----------------------------------------------------------------- helpers
async def _load(session, change_id: int, *, detail: bool = True) -> Change:
    stmt = (
        change_service.detail_query() if detail else change_service.base_query()
    ).where(Change.id == change_id)
    change = (await session.execute(stmt)).scalars().unique().one_or_none()
    if change is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Change not found."
        )
    return change


def _permissions(change: Change, user: User, config: dict) -> dict[str, bool]:
    """What this user may do with this change, in its current state."""
    is_admin = user.role_name == RoleName.ADMIN.value
    owns = change.requester_id == user.id
    editable_states = (ChangeStatus.DRAFT.value, ChangeStatus.PENDING_APPROVAL.value)

    return {
        "requires_approval": change_service.requires_approval(change, config),
        "can_edit": (
            change.status in editable_states
            and (owns or is_admin)
            and user.has_permission(Permission.CHANGE_WRITE.value)
        ),
        "can_submit": (
            change.status == ChangeStatus.DRAFT.value
            and (owns or is_admin)
            and user.has_permission(Permission.CHANGE_WRITE.value)
        ),
        "can_approve": (
            change.status == ChangeStatus.PENDING_APPROVAL.value
            and user.has_permission(Permission.CHANGE_APPROVE.value)
            # Approving your own request defeats the point of approval.
            and not owns
        ),
        "can_deploy": (
            change.status == ChangeStatus.APPROVED.value
            and user.has_permission(Permission.CHANGE_DEPLOY.value)
        ),
        "can_finish": (
            change.is_deploying and change_service.can_deploy(change, user)
        ),
        "can_cancel": (
            change.is_open
            and not change.is_deploying
            and (owns or is_admin)
        ),
        "can_comment": user.has_permission(Permission.CHANGE_COMMENT.value),
    }


def _read(change: Change, user: User, config: dict) -> ChangeRead:
    return to_read(change, permissions=_permissions(change, user, config))


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# -------------------------------------------------------------- dashboard
@router.get(
    "/dashboard",
    response_model=ChangeDashboard,
    summary="Change management overview",
)
async def change_dashboard(
    session: DbSession, _user: CanRead, config: RuntimeConfig
) -> ChangeDashboard:
    data = await change_service.dashboard(session, config)
    return ChangeDashboard(
        pending_approval=data["pending_approval"],
        approved=data["approved"],
        active_deployments=data["active_deployments"],
        completed_today=data["completed_today"],
        failed_today=data["failed_today"],
        draft=data["draft"],
        max_pause_minutes=data["max_pause_minutes"],
        upcoming=[to_list_item(c) for c in data["upcoming"]],
        active=[to_list_item(c) for c in data["active"]],
        overrunning=[to_list_item(c) for c in data["overrunning"]],
    )


@router.get(
    "/options", response_model=ChangeOptions, summary="Filter and form options"
)
async def change_options(
    session: DbSession, _user: CanRead, config: RuntimeConfig
) -> ChangeOptions:
    return ChangeOptions(
        applications=await change_service.applications(session),
        approval_environments=[
            str(name) for name in (config.get("change_approval_environments") or [])
        ],
    )


# ------------------------------------------------------------------- list
@router.get("", response_model=Page[ChangeListItem], summary="List changes")
async def list_changes(
    session: DbSession,
    user: CanRead,
    page: Pagination,
    search: Annotated[str | None, Query()] = None,
    change_status: Annotated[list[str] | None, Query(alias="status")] = None,
    application: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    risk: Annotated[list[str] | None, Query()] = None,
    mine: Annotated[
        bool, Query(description="Only changes I raised.")
    ] = False,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> Page[ChangeListItem]:
    """Paginated change list.

    ``mine=true`` backs the "My Changes" view; ``status=pending_approval``
    backs "Pending Approval". Both are the same endpoint so filters compose.
    """
    stmt = change_service.apply_filters(
        change_service.base_query(),
        search=search,
        statuses=split_csv_param(change_status),
        applications=split_csv_param(application),
        environment_ids=parse_uuid_list(environment),
        risks=split_csv_param(risk),
        requester_id=user.id if mine else None,
        since=since,
        until=until,
    )
    total = await change_service.count_query(session, stmt)

    ordering = (
        Change.expected_start_at.asc()
        if sort_dir == "asc"
        else Change.expected_start_at.desc()
    )
    rows = list(
        (
            await session.execute(
                stmt.order_by(ordering, Change.id.desc())
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


# ----------------------------------------------------------------- create
@router.post(
    "",
    response_model=ChangeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a change request",
)
async def create_change(
    payload: ChangeCreate,
    user: CanWrite,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    """Any signed-in user can raise a change. It starts as a draft."""
    try:
        change = await change_service.create(
            session, payload.model_dump(), user=user
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_CREATED.value,
        user=user,
        resource_type="change",
        resource_id=change.id,
        resource_name=change.reference,
        details={
            "application": change.application,
            "environment": change.environment_name,
            "risk": change.risk,
            "endpoints": len(change.endpoints),
        },
        request=request,
    )
    await session.commit()
    change = await _load(session, change.id)
    return _read(change, user, config)


@router.get("/{change_id}", response_model=ChangeRead, summary="Change details")
async def get_change(
    change_id: int, session: DbSession, user: CanRead, config: RuntimeConfig
) -> ChangeRead:
    return _read(await _load(session, change_id), user, config)


@router.put("/{change_id}", response_model=ChangeRead, summary="Edit a change")
async def update_change(
    change_id: int,
    payload: ChangeUpdate,
    user: CanWrite,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    change = await _load(session, change_id)
    rights = _permissions(change, user, config)
    if not rights["can_edit"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the requester or an administrator can edit this change, "
                "and only before it is approved."
            ),
        )
    try:
        await change_service.update_change(
            session, change, payload.model_dump(exclude_unset=True), user=user
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_UPDATED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)


# --------------------------------------------------------------- workflow
@router.post(
    "/{change_id}/submit",
    response_model=ChangeRead,
    summary="Submit for approval",
)
async def submit_change(
    change_id: int,
    user: CanWrite,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    """Submit a draft.

    Environments listed in ``change_approval_environments`` (production by
    default) move to PENDING APPROVAL; the rest are approved immediately, so a
    dev deployment is not blocked on ceremony.
    """
    change = await _load(session, change_id)
    if not _permissions(change, user, config)["can_submit"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the requester or an administrator can submit this change.",
        )
    try:
        await change_service.submit(session, change, user=user, config=config)
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_SUBMITTED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference,
        details={"resulting_status": change.status}, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)


@router.post(
    "/{change_id}/approve", response_model=ChangeRead, summary="Approve a change"
)
async def approve_change(
    change_id: int,
    payload: ApprovalRequest,
    user: CanApprove,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    change = await _load(session, change_id)
    try:
        await change_service.approve(
            session, change, user=user, comment=payload.comment
        )
        if payload.comment:
            await change_service.add_comment(
                session, change, user=user, body=payload.comment
            )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_APPROVED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)


@router.post(
    "/{change_id}/reject", response_model=ChangeRead, summary="Reject a change"
)
async def reject_change(
    change_id: int,
    payload: RejectionRequest,
    user: CanApprove,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    change = await _load(session, change_id)
    try:
        await change_service.reject(session, change, user=user, reason=payload.reason)
        await change_service.add_comment(
            session, change, user=user, body=f"Rejected: {payload.reason}"
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_REJECTED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference,
        details={"reason": payload.reason}, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)


@router.post(
    "/{change_id}/cancel", response_model=ChangeRead, summary="Cancel a change"
)
async def cancel_change(
    change_id: int,
    payload: CancelRequest,
    user: CanWrite,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    change = await _load(session, change_id)
    if not _permissions(change, user, config)["can_cancel"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the requester or an administrator can cancel this change.",
        )
    try:
        await change_service.cancel(
            session, change, user=user, reason=payload.reason
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_CANCELLED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)


# ------------------------------------------------------------- deployment
@router.post(
    "/{change_id}/start-deployment",
    response_model=DeploymentResult,
    summary="Start the deployment and pause affected monitoring",
)
async def start_deployment(
    change_id: int,
    user: CanDeploy,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> DeploymentResult:
    """Begin a deployment.

    The deployer is taken from the authenticated session and is never
    accepted from the request body. Monitoring for the affected endpoints is
    paused immediately, so the deployment window produces no incidents, no
    alerts, and no downtime in the availability figures.
    """
    change = await _load(session, change_id)
    try:
        change, paused = await change_service.start_deployment(
            session, change, user=user, config=config
        )
    except DuplicateDeployment as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.DEPLOYMENT_STARTED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference,
        details={
            "application": change.application,
            "environment": change.environment_name,
            "endpoints_paused": len([p for p in paused if not p["was_paused_before"]]),
        },
        request=request,
    )
    if paused:
        await audit_service.record(
            session,
            action=AuditAction.MONITORING_PAUSED.value,
            user=user, resource_type="change", resource_id=change.id,
            resource_name=change.reference,
            details={"endpoints": [p["name"] for p in paused]},
            request=request,
        )
    await session.commit()

    reloaded = await _load(session, change_id)
    return DeploymentResult(
        change=_read(reloaded, user, config), monitoring_paused=paused
    )


@router.post(
    "/{change_id}/complete",
    response_model=DeploymentResult,
    summary="Complete the deployment, resume monitoring and health-check",
)
async def complete_deployment(
    change_id: int,
    payload: CompleteDeploymentRequest,
    user: CanDeploy,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> DeploymentResult:
    """Finish a deployment.

    Resumes only the endpoints this change paused, then immediately checks
    them so a deployment that broke something is visible right away rather
    than at the next scheduled check.
    """
    change = await _load(session, change_id)
    if not change_service.can_deploy(change, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the person who started this deployment, or an "
                "administrator, can complete it."
            ),
        )
    try:
        result = await change_service.complete_deployment(
            session, change, user=user, config=config,
            notes=payload.deployment_notes,
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.DEPLOYMENT_COMPLETED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference,
        details={"endpoints_resumed": len(result["resumed"])},
        request=request,
    )
    await audit_service.record(
        session,
        action=AuditAction.MONITORING_RESUMED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()

    reloaded = await _load(session, change_id)
    return DeploymentResult(
        change=_read(reloaded, user, config),
        monitoring_resumed=result["resumed"],
        health_check=result["health_check"],
    )


@router.post(
    "/{change_id}/fail",
    response_model=DeploymentResult,
    summary="Mark the deployment failed, resume monitoring and health-check",
)
async def fail_deployment(
    change_id: int,
    payload: FailDeploymentRequest,
    user: CanDeploy,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> DeploymentResult:
    change = await _load(session, change_id)
    if not change_service.can_deploy(change, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the person who started this deployment, or an "
                "administrator, can mark it failed."
            ),
        )
    try:
        result = await change_service.fail_deployment(
            session, change, user=user, config=config,
            reason=payload.reason, notes=payload.deployment_notes,
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.DEPLOYMENT_FAILED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference,
        details={
            "reason": payload.reason,
            "endpoints_resumed": len(result["resumed"]),
        },
        request=request,
    )
    await audit_service.record(
        session,
        action=AuditAction.MONITORING_RESUMED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()

    reloaded = await _load(session, change_id)
    return DeploymentResult(
        change=_read(reloaded, user, config),
        monitoring_resumed=result["resumed"],
        health_check=result["health_check"],
    )


# --------------------------------------------------------------- comments
@router.post(
    "/{change_id}/comments",
    response_model=ChangeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a change",
)
async def add_comment(
    change_id: int,
    payload: CommentCreate,
    user: CanComment,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ChangeRead:
    """Anyone who can see the change can comment - no extra permission."""
    change = await _load(session, change_id)
    try:
        await change_service.add_comment(
            session, change, user=user, body=payload.body
        )
    except ChangeError as exc:
        raise _bad(exc) from exc

    await audit_service.record(
        session,
        action=AuditAction.CHANGE_COMMENTED.value,
        user=user, resource_type="change", resource_id=change.id,
        resource_name=change.reference, request=request,
    )
    await session.commit()
    return _read(await _load(session, change_id), user, config)
