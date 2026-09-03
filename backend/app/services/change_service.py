"""Change management workflow.

The whole feature exists to give a small team three things:

1. approval before a production deployment,
2. a trustworthy record of who deployed what and when, and
3. automatic coordination between deployments and endpoint monitoring.

The monitoring coordination is the part worth being careful about. Starting a
deployment pauses the affected endpoints, and because the worker's claim query
already skips paused endpoints, no checks run at all while a deployment is in
flight - so no incidents open, no alerts fire, and the deployment window never
lands in the uptime figures. Historical data is untouched.

The pause is *attributed*: an endpoint records which change paused it, and the
link row remembers whether it was already paused beforehand. Completing a
deployment therefore only resumes the endpoints that this change actually
paused, and never re-enables one an operator had deliberately turned off.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import (
    ACTIVE_DEPLOYMENT_STATUSES,
    TERMINAL_CHANGE_STATUSES,
    ChangeAction,
    ChangeRisk,
    ChangeStatus,
    EndpointStatus,
)
from app.core.logging import get_logger
from app.models.change import Change, ChangeActivity, ChangeComment, change_endpoints
from app.models.endpoint import Endpoint, Environment
from app.models.user import User
from app.services import endpoint_service, monitoring_service

logger = get_logger(__name__)

# The post-deployment health check runs against every affected endpoint at
# once; bounded so a change touching 80 endpoints cannot stall the request.
HEALTH_CHECK_CONCURRENCY = 10
HEALTH_CHECK_MAX_ENDPOINTS = 40


class ChangeError(ValueError):
    """A workflow rule was violated. The message is safe to show a user."""


class DuplicateDeployment(ChangeError):
    def __init__(self, other: Change) -> None:
        super().__init__(
            f"{other.application} / {other.environment_name or 'no environment'} "
            f"already has a deployment in progress ({other.reference}), started by "
            f"{other.deployer_name or 'someone'} at "
            f"{other.started_at.strftime('%H:%M') if other.started_at else 'unknown'}."
        )
        self.other = other


def _now() -> datetime:
    return datetime.now(timezone.utc)


def base_query() -> Select:
    return select(Change).options(
        selectinload(Change.environment),
        selectinload(Change.endpoints),
    )


def detail_query() -> Select:
    return base_query().options(
        selectinload(Change.comments),
        selectinload(Change.activity),
    )


# ------------------------------------------------------------- references
async def next_reference(session: AsyncSession, *, year: int | None = None) -> str:
    """Allocate the next ``CHG-<year>-<seq>`` identifier.

    Derived from the highest existing reference for the year rather than a
    sequence, so the numbering stays readable and gap-free in normal use. The
    unique constraint on ``reference`` is the real guard - :func:`create`
    retries on collision, which is what makes this safe under concurrency.
    """
    year = year or _now().year
    prefix = f"CHG-{year}-"
    highest = (
        await session.execute(
            select(func.max(Change.reference)).where(
                Change.reference.like(f"{prefix}%")
            )
        )
    ).scalar()

    sequence = 1
    if highest:
        try:
            sequence = int(str(highest).rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = 1
    return f"{prefix}{sequence:04d}"


# ------------------------------------------------------------- activity
async def record_activity(
    session: AsyncSession,
    change: Change,
    *,
    action: str,
    detail: str | None = None,
    user: User | None = None,
) -> ChangeActivity:
    entry = ChangeActivity(
        change_id=change.id,
        user_id=user.id if user else None,
        username=user.username if user else None,
        action=action,
        detail=detail,
    )
    session.add(entry)
    await session.flush()
    return entry


# ---------------------------------------------------------------- create
async def _resolve_endpoints(
    session: AsyncSession, endpoint_ids: Iterable[uuid.UUID] | None
) -> list[Endpoint]:
    ids = [i for i in (endpoint_ids or [])]
    if not ids:
        return []
    rows = (
        await session.execute(
            endpoint_service.base_query().where(Endpoint.id.in_(ids))
        )
    ).scalars().unique().all()
    found = {row.id for row in rows}
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise ChangeError("unknown endpoint(s): " + ", ".join(missing))
    return list(rows)


async def create(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    user: User,
) -> Change:
    """Create a change request in DRAFT."""
    title = (payload.get("title") or "").strip()
    application = (payload.get("application") or "").strip()
    description = (payload.get("description") or "").strip()
    if not title:
        raise ChangeError("a title is required")
    if not application:
        raise ChangeError("an application is required")
    if not description:
        raise ChangeError("a description is required")

    risk = str(payload.get("risk") or ChangeRisk.LOW.value).lower()
    if risk not in {r.value for r in ChangeRisk}:
        raise ChangeError("risk must be low, medium or high")

    expected_start = payload.get("expected_start_at")
    if not isinstance(expected_start, datetime):
        raise ChangeError("an expected deployment date and time is required")
    if expected_start.tzinfo is None:
        expected_start = expected_start.replace(tzinfo=timezone.utc)

    duration = int(payload.get("expected_duration_minutes") or 30)
    if duration < 1 or duration > 24 * 60:
        raise ChangeError("expected duration must be between 1 and 1440 minutes")

    environment = await endpoint_service.resolve_environment(
        session, payload.get("environment")
    )
    endpoints = await _resolve_endpoints(session, payload.get("endpoint_ids"))

    change = Change(
        title=title[:200],
        application=application[:128],
        environment_id=environment.id if environment else None,
        description=description,
        expected_start_at=expected_start,
        expected_duration_minutes=duration,
        risk=risk,
        rollback_plan=(payload.get("rollback_plan") or None),
        deployment_notes=(payload.get("deployment_notes") or None),
        status=ChangeStatus.DRAFT.value,
        requester_id=user.id,
        requester_name=user.username,
    )
    change.endpoints = endpoints

    # The unique constraint on `reference` is the real guard against two
    # requests picking the same number; retry a few times on collision.
    for attempt in range(5):
        change.reference = await next_reference(session)
        try:
            async with session.begin_nested():
                session.add(change)
                await session.flush()
            break
        except IntegrityError:
            if attempt == 4:
                raise ChangeError(
                    "could not allocate a change reference - try again"
                ) from None
            continue

    await record_activity(
        session, change, action=ChangeAction.CREATED.value,
        detail=f"Change created by {user.username}", user=user,
    )
    logger.info(
        "change_created",
        reference=change.reference, application=change.application,
        environment=change.environment_name, by=user.username,
    )
    return change


async def update_change(
    session: AsyncSession, change: Change, payload: dict[str, Any], *, user: User
) -> Change:
    """Edit a change. Only possible before it is approved."""
    if change.status not in (
        ChangeStatus.DRAFT.value,
        ChangeStatus.PENDING_APPROVAL.value,
    ):
        raise ChangeError(
            f"a change in '{change.status}' can no longer be edited"
        )

    if "title" in payload and payload["title"]:
        change.title = str(payload["title"]).strip()[:200]
    if "application" in payload and payload["application"]:
        change.application = str(payload["application"]).strip()[:128]
    if "description" in payload and payload["description"]:
        change.description = str(payload["description"]).strip()
    if "risk" in payload and payload["risk"]:
        risk = str(payload["risk"]).lower()
        if risk not in {r.value for r in ChangeRisk}:
            raise ChangeError("risk must be low, medium or high")
        change.risk = risk
    if payload.get("expected_start_at"):
        value = payload["expected_start_at"]
        change.expected_start_at = (
            value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        )
    if payload.get("expected_duration_minutes"):
        change.expected_duration_minutes = int(payload["expected_duration_minutes"])
    for field in ("rollback_plan", "deployment_notes"):
        if field in payload:
            setattr(change, field, payload[field] or None)
    if "environment" in payload:
        environment = await endpoint_service.resolve_environment(
            session, payload["environment"]
        )
        change.environment_id = environment.id if environment else None
    if "endpoint_ids" in payload and payload["endpoint_ids"] is not None:
        change.endpoints = await _resolve_endpoints(session, payload["endpoint_ids"])

    await session.flush()
    await record_activity(
        session, change, action=ChangeAction.UPDATED.value,
        detail=f"Change updated by {user.username}", user=user,
    )
    return change


# ------------------------------------------------------------- approval
def requires_approval(change: Change, config: dict[str, Any]) -> bool:
    """Whether this change must be approved before it can be deployed."""
    required = [
        str(name).lower()
        for name in (config.get("change_approval_environments") or [])
    ]
    return (change.environment_name or "").lower() in required


async def submit(
    session: AsyncSession, change: Change, *, user: User, config: dict[str, Any]
) -> Change:
    """Submit for approval - or move straight to APPROVED where the
    environment does not require it."""
    if change.status != ChangeStatus.DRAFT.value:
        raise ChangeError(f"only a draft can be submitted (this one is '{change.status}')")

    if requires_approval(change, config):
        change.status = ChangeStatus.PENDING_APPROVAL.value
        await record_activity(
            session, change, action=ChangeAction.SUBMITTED.value,
            detail=(
                f"Submitted for approval by {user.username} - "
                f"{change.environment_name} requires approval"
            ),
            user=user,
        )
    else:
        change.status = ChangeStatus.APPROVED.value
        change.approved_at = _now()
        await record_activity(
            session, change, action=ChangeAction.APPROVED.value,
            detail=(
                f"Auto-approved: {change.environment_name or 'this environment'} "
                "does not require approval"
            ),
            user=user,
        )
    await session.flush()
    return change


async def approve(
    session: AsyncSession, change: Change, *, user: User, comment: str | None = None
) -> Change:
    if change.status != ChangeStatus.PENDING_APPROVAL.value:
        raise ChangeError(
            f"only a change pending approval can be approved (this one is "
            f"'{change.status}')"
        )
    if change.requester_id and change.requester_id == user.id:
        raise ChangeError("you cannot approve your own change request")

    change.status = ChangeStatus.APPROVED.value
    change.approver_id = user.id
    change.approver_name = user.username
    change.approved_at = _now()
    change.rejection_reason = None
    await session.flush()
    await record_activity(
        session, change, action=ChangeAction.APPROVED.value,
        detail=(f"Approved by {user.username}" + (f": {comment}" if comment else "")),
        user=user,
    )
    logger.info("change_approved", reference=change.reference, by=user.username)
    return change


async def reject(
    session: AsyncSession, change: Change, *, user: User, reason: str
) -> Change:
    if change.status != ChangeStatus.PENDING_APPROVAL.value:
        raise ChangeError(
            f"only a change pending approval can be rejected (this one is "
            f"'{change.status}')"
        )
    reason = (reason or "").strip()
    if not reason:
        raise ChangeError("a rejection reason is required")

    change.status = ChangeStatus.REJECTED.value
    change.approver_id = user.id
    change.approver_name = user.username
    change.rejection_reason = reason
    await session.flush()
    await record_activity(
        session, change, action=ChangeAction.REJECTED.value,
        detail=f"Rejected by {user.username}: {reason}", user=user,
    )
    return change


async def cancel(
    session: AsyncSession, change: Change, *, user: User, reason: str | None = None
) -> Change:
    if change.status in TERMINAL_CHANGE_STATUSES:
        raise ChangeError(f"a change in '{change.status}' cannot be cancelled")
    if change.is_deploying:
        raise ChangeError(
            "a deployment in progress cannot be cancelled - complete it or mark "
            "it failed so monitoring resumes"
        )

    change.status = ChangeStatus.CANCELLED.value
    await session.flush()
    await record_activity(
        session, change, action=ChangeAction.CANCELLED.value,
        detail=(f"Cancelled by {user.username}" + (f": {reason}" if reason else "")),
        user=user,
    )
    return change


# ----------------------------------------------------------- deployment
async def active_deployment_for(
    session: AsyncSession, *, application: str, environment_id: uuid.UUID | None,
    exclude_id: int | None = None,
) -> Change | None:
    """Find a deployment already running for this application/environment."""
    stmt = base_query().where(
        func.lower(Change.application) == application.lower(),
        Change.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
    )
    if environment_id is None:
        stmt = stmt.where(Change.environment_id.is_(None))
    else:
        stmt = stmt.where(Change.environment_id == environment_id)
    if exclude_id is not None:
        stmt = stmt.where(Change.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalars().unique().one_or_none()


async def _pause_endpoints(
    session: AsyncSession, change: Change
) -> list[dict[str, Any]]:
    """Pause the affected endpoints, remembering their prior state."""
    paused: list[dict[str, Any]] = []
    for endpoint in change.endpoints:
        was_paused = bool(endpoint.is_paused)
        await session.execute(
            update(change_endpoints)
            .where(
                change_endpoints.c.change_id == change.id,
                change_endpoints.c.endpoint_id == endpoint.id,
            )
            .values(was_paused_before=was_paused)
        )
        if not was_paused:
            endpoint.is_paused = True
            endpoint.current_status = EndpointStatus.PAUSED.value
            # Clearing the failure streak stops a deployment-time blip from
            # counting toward the incident threshold once checks resume.
            endpoint.consecutive_failures = 0
            endpoint.next_check_at = None
        endpoint.pause_reason = f"Deployment {change.reference}"
        endpoint.paused_by_change_id = change.id
        paused.append(
            {
                "endpoint_id": str(endpoint.id),
                "name": endpoint.name,
                "was_paused_before": was_paused,
            }
        )
    await session.flush()
    return paused


async def _resume_endpoints(
    session: AsyncSession, change: Change
) -> list[dict[str, Any]]:
    """Resume only what this change actually paused."""
    prior = {
        row.endpoint_id: row.was_paused_before
        for row in (
            await session.execute(
                select(
                    change_endpoints.c.endpoint_id,
                    change_endpoints.c.was_paused_before,
                ).where(change_endpoints.c.change_id == change.id)
            )
        ).all()
    }

    resumed: list[dict[str, Any]] = []
    for endpoint in change.endpoints:
        was_paused_before = bool(prior.get(endpoint.id, False))
        # Someone else's pause (or a later change's) must not be lifted here.
        owned = endpoint.paused_by_change_id == change.id
        if owned:
            endpoint.pause_reason = None
            endpoint.paused_by_change_id = None
        if owned and not was_paused_before:
            endpoint.is_paused = False
            endpoint.current_status = EndpointStatus.UNKNOWN.value
            endpoint.next_check_at = _now()
            resumed.append({"endpoint_id": str(endpoint.id), "name": endpoint.name})
    await session.flush()
    return resumed


async def start_deployment(
    session: AsyncSession, change: Change, *, user: User, config: dict[str, Any]
) -> tuple[Change, list[dict[str, Any]]]:
    """Begin a deployment: record the deployer and pause the monitoring."""
    if change.status != ChangeStatus.APPROVED.value:
        if change.status == ChangeStatus.PENDING_APPROVAL.value:
            raise ChangeError("this change is still awaiting approval")
        raise ChangeError(
            f"only an approved change can be deployed (this one is '{change.status}')"
        )

    clash = await active_deployment_for(
        session,
        application=change.application,
        environment_id=change.environment_id,
        exclude_id=change.id,
    )
    if clash is not None:
        raise DuplicateDeployment(clash)

    change.status = ChangeStatus.DEPLOYMENT_IN_PROGRESS.value
    # Taken from the authenticated session, never from the request body.
    change.deployer_id = user.id
    change.deployer_name = user.username
    change.started_at = _now()
    change.completed_at = None
    change.failure_reason = None
    await session.flush()

    paused = await _pause_endpoints(session, change)

    await record_activity(
        session, change, action=ChangeAction.DEPLOYMENT_STARTED.value,
        detail=f"Deployment started by {user.username}", user=user,
    )
    if paused:
        newly = [p for p in paused if not p["was_paused_before"]]
        await record_activity(
            session, change, action=ChangeAction.MONITORING_PAUSED.value,
            detail=(
                f"Monitoring paused for {len(newly)} endpoint(s)"
                + (
                    f"; {len(paused) - len(newly)} were already paused"
                    if len(newly) != len(paused)
                    else ""
                )
            ),
        )
    logger.info(
        "deployment_started",
        reference=change.reference, deployer=user.username,
        endpoints_paused=len(paused),
    )
    return change, paused


async def _run_health_check(
    session: AsyncSession, change: Change, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Check the affected endpoints immediately after monitoring resumes.

    Results ARE recorded: a deployment that broke something should show up in
    the history straight away rather than at the next scheduled check.
    """
    endpoints = list(change.endpoints)[:HEALTH_CHECK_MAX_ENDPOINTS]
    if not endpoints:
        return []

    semaphore = asyncio.Semaphore(HEALTH_CHECK_CONCURRENCY)

    async def _one(endpoint: Endpoint) -> dict[str, Any]:
        async with semaphore:
            try:
                outcome = await monitoring_service.execute_check(endpoint, config)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "post_deploy_check_failed",
                    endpoint=endpoint.name, error=str(exc),
                )
                return {
                    "endpoint_id": str(endpoint.id),
                    "name": endpoint.name,
                    "status": "error",
                    "detail": str(exc)[:200],
                }
            return {
                "endpoint_id": str(endpoint.id),
                "name": endpoint.name,
                "url": endpoint.url,
                "status": outcome.status,
                "http_status": outcome.http_status_code,
                "response_time_ms": outcome.response_time_ms,
                "ssl_status": outcome.ssl_status,
                "ssl_days_remaining": outcome.ssl_days_remaining,
                "error": outcome.error_message,
                "outcome": outcome,
            }

    results = await asyncio.gather(*(_one(e) for e in endpoints))

    # Persist sequentially: record_check_result mutates the endpoint row and
    # may open or close an incident, so it must not run concurrently.
    summary: list[dict[str, Any]] = []
    for endpoint, result in zip(endpoints, results):
        outcome = result.pop("outcome", None)
        if outcome is not None:
            try:
                await monitoring_service.record_check_result(
                    session, endpoint, outcome,
                    config=config, checked_by=f"deploy:{change.reference}",
                    is_manual=True,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "post_deploy_record_failed",
                    endpoint=endpoint.name, error=str(exc),
                )
            endpoint.next_check_at = monitoring_service.next_check_time(
                endpoint.interval_seconds
            )
        summary.append(result)

    await session.flush()
    return summary


async def _finish(
    session: AsyncSession,
    change: Change,
    *,
    user: User,
    config: dict[str, Any],
    status: str,
    notes: str | None,
    failure_reason: str | None,
) -> dict[str, Any]:
    change.status = status
    change.completed_at = _now()
    if notes:
        change.deployment_notes = notes
    if failure_reason:
        change.failure_reason = failure_reason
    await session.flush()

    resumed = await _resume_endpoints(session, change)

    action = (
        ChangeAction.DEPLOYMENT_COMPLETED.value
        if status == ChangeStatus.COMPLETED.value
        else ChangeAction.DEPLOYMENT_FAILED.value
    )
    await record_activity(
        session, change, action=action,
        detail=(
            f"Deployment {'completed' if action.endswith('completed') else 'marked failed'}"
            f" by {user.username}"
            + (f": {failure_reason}" if failure_reason else "")
        ),
        user=user,
    )
    await record_activity(
        session, change, action=ChangeAction.MONITORING_RESUMED.value,
        detail=f"Monitoring resumed for {len(resumed)} endpoint(s)",
    )

    health: list[dict[str, Any]] = []
    if config.get("change_health_check_on_resume", True):
        health = await _run_health_check(session, change, config)
        change.health_check = health
        if health:
            healthy = sum(1 for h in health if h.get("status") == "up")
            await record_activity(
                session, change, action=ChangeAction.HEALTH_CHECK.value,
                detail=(
                    f"Post-deployment health check: {healthy} of {len(health)} "
                    "endpoint(s) healthy"
                ),
            )

    await session.flush()
    logger.info(
        "deployment_finished",
        reference=change.reference, status=status,
        by=user.username, endpoints_resumed=len(resumed),
    )
    return {"resumed": resumed, "health_check": health}


async def complete_deployment(
    session: AsyncSession, change: Change, *, user: User, config: dict[str, Any],
    notes: str | None = None,
) -> dict[str, Any]:
    if not change.is_deploying:
        raise ChangeError(
            f"only a deployment in progress can be completed (this one is "
            f"'{change.status}')"
        )
    return await _finish(
        session, change, user=user, config=config,
        status=ChangeStatus.COMPLETED.value, notes=notes, failure_reason=None,
    )


async def fail_deployment(
    session: AsyncSession, change: Change, *, user: User, config: dict[str, Any],
    reason: str, notes: str | None = None,
) -> dict[str, Any]:
    if not change.is_deploying:
        raise ChangeError(
            f"only a deployment in progress can be marked failed (this one is "
            f"'{change.status}')"
        )
    reason = (reason or "").strip()
    if not reason:
        raise ChangeError("a failure reason is required")
    return await _finish(
        session, change, user=user, config=config,
        status=ChangeStatus.FAILED.value, notes=notes, failure_reason=reason,
    )


def can_deploy(change: Change, user: User) -> bool:
    """Whether this user may start or finish this deployment.

    Any change:deploy holder can start one. Finishing is restricted to the
    person who started it or an administrator, so a deployment cannot be
    closed out by a bystander who does not know whether it worked.
    """
    from app.core.enums import Permission, RoleName

    if not user.has_permission(Permission.CHANGE_DEPLOY.value):
        return False
    if change.deployer_id and change.deployer_id != user.id:
        return user.role_name == RoleName.ADMIN.value
    return True


# ------------------------------------------------------------- comments
async def add_comment(
    session: AsyncSession, change: Change, *, user: User, body: str
) -> ChangeComment:
    """Anyone who can see the change can comment on it."""
    body = (body or "").strip()
    if not body:
        raise ChangeError("a comment cannot be empty")
    if len(body) > 4000:
        raise ChangeError("a comment must be at most 4000 characters")

    comment = ChangeComment(
        change_id=change.id,
        user_id=user.id,
        username=user.username,
        body=body,
    )
    session.add(comment)
    await session.flush()
    return comment


# ------------------------------------------------------------- listings
def apply_filters(
    stmt: Select,
    *,
    search: str | None = None,
    statuses: Sequence[str] | None = None,
    applications: Sequence[str] | None = None,
    environment_ids: Sequence[uuid.UUID] | None = None,
    risks: Sequence[str] | None = None,
    requester_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Select:
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Change.reference).like(needle),
                func.lower(Change.title).like(needle),
                func.lower(Change.application).like(needle),
                func.lower(func.coalesce(Change.description, "")).like(needle),
                func.lower(func.coalesce(Change.requester_name, "")).like(needle),
            )
        )
    if statuses:
        stmt = stmt.where(Change.status.in_(list(statuses)))
    if applications:
        stmt = stmt.where(Change.application.in_(list(applications)))
    if environment_ids:
        stmt = stmt.where(Change.environment_id.in_(list(environment_ids)))
    if risks:
        stmt = stmt.where(Change.risk.in_(list(risks)))
    if requester_id is not None:
        stmt = stmt.where(Change.requester_id == requester_id)
    if since is not None:
        stmt = stmt.where(Change.expected_start_at >= since)
    if until is not None:
        stmt = stmt.where(Change.expected_start_at <= until)
    return stmt


async def count_query(session: AsyncSession, stmt: Select) -> int:
    subquery = stmt.with_only_columns(Change.id).order_by(None).subquery()
    return int(
        (await session.execute(select(func.count()).select_from(subquery))).scalar() or 0
    )


async def dashboard(session: AsyncSession, config: dict[str, Any]) -> dict[str, Any]:
    """Counts and lists for the change management overview."""
    now = _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    status_rows = (
        await session.execute(
            select(Change.status, func.count(Change.id)).group_by(Change.status)
        )
    ).all()
    counts = {str(s): int(c) for s, c in status_rows}

    completed_today = int(
        (
            await session.execute(
                select(func.count(Change.id)).where(
                    Change.status == ChangeStatus.COMPLETED.value,
                    Change.completed_at >= day_start,
                )
            )
        ).scalar()
        or 0
    )
    failed_today = int(
        (
            await session.execute(
                select(func.count(Change.id)).where(
                    Change.status == ChangeStatus.FAILED.value,
                    Change.completed_at >= day_start,
                )
            )
        ).scalar()
        or 0
    )

    upcoming = list(
        (
            await session.execute(
                base_query()
                .where(
                    Change.status.in_(
                        [
                            ChangeStatus.APPROVED.value,
                            ChangeStatus.PENDING_APPROVAL.value,
                        ]
                    ),
                    Change.expected_start_at >= now - timedelta(hours=2),
                )
                .order_by(Change.expected_start_at.asc())
                .limit(10)
            )
        ).scalars().unique().all()
    )

    active = list(
        (
            await session.execute(
                base_query()
                .where(Change.status.in_(ACTIVE_DEPLOYMENT_STATUSES))
                .order_by(Change.started_at.asc())
            )
        ).scalars().unique().all()
    )

    # Safety net: a deployment left running silences its endpoints.
    max_pause = int(config.get("change_max_pause_minutes", 240))
    overrunning = [
        change
        for change in active
        if change.started_at
        and (now - change.started_at).total_seconds() > max_pause * 60
    ]

    return {
        "pending_approval": counts.get(ChangeStatus.PENDING_APPROVAL.value, 0),
        "approved": counts.get(ChangeStatus.APPROVED.value, 0),
        "active_deployments": len(active),
        "completed_today": completed_today,
        "failed_today": failed_today,
        "draft": counts.get(ChangeStatus.DRAFT.value, 0),
        "upcoming": upcoming,
        "active": active,
        "overrunning": overrunning,
        "max_pause_minutes": max_pause,
    }


async def applications(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            select(Change.application).distinct().order_by(Change.application)
        )
    ).scalars().all()
    return [row for row in rows if row]
