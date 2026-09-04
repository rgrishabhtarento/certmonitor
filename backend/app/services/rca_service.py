"""RCA workflow, ownership and analytics.

The governing rule is that **RCA is optional and never blocks anything**. It
does not gate incident resolution, incident closure, deployment completion or
monitoring restoration. An incident can be closed with its RCA at Pending, and
completing an RCA leaves the incident exactly where it was. A process that
holds up recovery for paperwork is a process people learn to route around, and
then the paperwork stops happening at all.

Ownership introduces no new roles. Anyone with ``incident:write`` can request,
assign and edit; the assigned owner - a person, or anyone whose team label
matches - can edit their own regardless of role, which is what lets a viewer
own an RCA. That is the whole permission model.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import (
    OPEN_RCA_STATUSES,
    Permission,
    RcaOwnerType,
    RcaStatus,
    RoleName,
    RootCauseCategory,
)
from app.core.logging import get_logger
from app.models.incident import Incident
from app.models.rca import IncidentComment, Rca
from app.models.user import User
from app.services import rca_draft

logger = get_logger(__name__)

ANALYTICS_WINDOW_DAYS = 90


class RcaError(ValueError):
    """A workflow rule was violated. The message is safe to show a user."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def base_query() -> Select:
    return select(Rca)


def detail_query() -> Select:
    return select(Rca).options(selectinload(Rca.incident))


async def count_query(session: AsyncSession, stmt: Select) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar()
        or 0
    )


# ------------------------------------------------------------ permissions
def can_edit(rca: Rca, user: User) -> bool:
    """Whether this user may change this RCA.

    Three ways in, and no new role for any of them: administrators, anyone
    who can write incidents, and the assigned owner. The last is what makes
    team ownership work - a viewer assigned an RCA can complete it, which is
    the whole point of assigning it to them.
    """
    if user.role_name == RoleName.ADMIN.value:
        return True
    if user.has_permission(Permission.INCIDENT_WRITE.value):
        return True
    return is_owner(rca, user)


def is_owner(rca: Rca, user: User) -> bool:
    if rca.owner_user_id and rca.owner_user_id == user.id:
        return True
    if rca.owner_team and getattr(user, "team", None):
        return rca.owner_team.strip().lower() == user.team.strip().lower()
    return False


def can_assign(user: User) -> bool:
    """Assigning is a management action, so it needs incident:write."""
    return user.role_name == RoleName.ADMIN.value or user.has_permission(
        Permission.INCIDENT_WRITE.value
    )


# ------------------------------------------------------------- workflow
async def get_for_incident(
    session: AsyncSession, incident_id: int
) -> Rca | None:
    return (
        await session.execute(select(Rca).where(Rca.incident_id == incident_id))
    ).scalars().first()


async def request_rca(
    session: AsyncSession,
    incident: Incident,
    *,
    user: User,
    owner_type: str | None = None,
    owner_user: User | None = None,
    owner_team: str | None = None,
    due_in_days: int | None = None,
) -> Rca:
    """Open an RCA against an incident.

    Idempotent: an existing RCA is returned rather than duplicated, and a
    previously-declined one can be reopened without losing what was written.
    """
    existing = await get_for_incident(session, incident.id)
    if existing is not None and existing.status not in (
        RcaStatus.NOT_REQUESTED.value,
        RcaStatus.NOT_REQUIRED.value,
    ):
        return existing

    endpoint = incident.endpoint
    rca = existing or Rca(incident_id=incident.id)
    rca.endpoint_id = incident.endpoint_id
    rca.endpoint_name = getattr(endpoint, "name", None)
    rca.application = getattr(endpoint, "application", None)
    rca.environment = (
        endpoint.environment.name
        if getattr(endpoint, "environment", None)
        else None
    )
    rca.status = RcaStatus.PENDING.value
    rca.requested_by_id = user.id
    rca.requested_by = user.username
    rca.requested_at = _now()
    rca.not_required_reason = None
    if due_in_days:
        rca.due_at = _now() + timedelta(days=int(due_in_days))

    _apply_owner(rca, owner_type=owner_type, owner_user=owner_user, owner_team=owner_team)

    if existing is None:
        session.add(rca)
    await session.flush()

    logger.info(
        "rca_requested",
        incident_id=incident.id, rca_id=rca.id,
        owner=rca.owner_label, by=user.username,
    )
    return rca


def _apply_owner(
    rca: Rca,
    *,
    owner_type: str | None,
    owner_user: User | None,
    owner_team: str | None,
) -> None:
    """Set ownership to a person or a team - never both at once."""
    if owner_type == RcaOwnerType.TEAM.value:
        team = (owner_team or "").strip()
        if not team:
            raise RcaError("a team name is required when assigning to a team")
        rca.owner_type = RcaOwnerType.TEAM.value
        rca.owner_team = team[:64]
        rca.owner_user_id = None
        rca.owner_user_name = None
    elif owner_type == RcaOwnerType.INDIVIDUAL.value:
        if owner_user is None:
            raise RcaError("a user is required when assigning to an individual")
        rca.owner_type = RcaOwnerType.INDIVIDUAL.value
        rca.owner_user_id = owner_user.id
        rca.owner_user_name = owner_user.username
        rca.owner_team = None
    elif owner_type is not None:
        raise RcaError("owner_type must be 'individual' or 'team'")


async def assign(
    session: AsyncSession,
    rca: Rca,
    *,
    user: User,
    owner_type: str,
    owner_user: User | None = None,
    owner_team: str | None = None,
    due_in_days: int | None = None,
) -> Rca:
    if rca.status == RcaStatus.NOT_REQUIRED.value:
        raise RcaError("this RCA is marked not required - request it again first")

    _apply_owner(rca, owner_type=owner_type, owner_user=owner_user, owner_team=owner_team)
    if due_in_days is not None:
        rca.due_at = _now() + timedelta(days=int(due_in_days)) if due_in_days else None
    if rca.status == RcaStatus.NOT_REQUESTED.value:
        rca.status = RcaStatus.PENDING.value
    await session.flush()

    logger.info(
        "rca_assigned", rca_id=rca.id, owner=rca.owner_label, by=user.username
    )
    return rca


async def start(session: AsyncSession, rca: Rca, *, user: User) -> Rca:
    if rca.status == RcaStatus.COMPLETED.value:
        raise RcaError("this RCA is already complete")
    if rca.status == RcaStatus.NOT_REQUIRED.value:
        raise RcaError("this RCA is marked not required")
    if rca.status != RcaStatus.IN_PROGRESS.value:
        rca.status = RcaStatus.IN_PROGRESS.value
        rca.started_at = rca.started_at or _now()
    await session.flush()
    return rca


async def save(
    session: AsyncSession, rca: Rca, payload: dict[str, Any], *, user: User
) -> Rca:
    """Update the RCA content.

    Saving does not complete it. Partial work is normal - an RCA written over
    three days by two people is the common case, not the exception.
    """
    if rca.status == RcaStatus.NOT_REQUIRED.value:
        raise RcaError("this RCA is marked not required - request it again first")

    for field in ("root_cause", "impact", "resolution"):
        if field in payload:
            value = payload[field]
            setattr(rca, field, (value or "").strip() or None)

    if "root_cause_category" in payload:
        category = payload["root_cause_category"]
        if category:
            category = str(category).strip().lower()
            if category not in {c.value for c in RootCauseCategory}:
                raise RcaError(f"unknown root cause category '{category}'")
            rca.root_cause_category = category
        else:
            rca.root_cause_category = None

    if "preventive_actions" in payload:
        actions = payload["preventive_actions"] or []
        cleaned = []
        for item in actions[:20]:
            text = str(item.get("text", "")).strip() if isinstance(item, dict) else str(item).strip()
            if text:
                cleaned.append({
                    "text": text[:500],
                    "done": bool(item.get("done")) if isinstance(item, dict) else False,
                })
        rca.preventive_actions = cleaned or None

    if "timeline" in payload and payload["timeline"] is not None:
        entries = []
        for item in (payload["timeline"] or [])[:100]:
            if not isinstance(item, dict):
                continue
            detail = str(item.get("detail", "")).strip()
            if not detail:
                continue
            entries.append({
                "at": item.get("at"),
                "kind": str(item.get("kind") or "note")[:32],
                "detail": detail[:1000],
                "source": str(item.get("source") or "manual")[:16],
            })
        rca.timeline = entries or None

    if "due_at" in payload:
        rca.due_at = payload["due_at"]

    # Writing content is starting work. Making the operator click a separate
    # "start" button first would only produce RCAs stuck at Pending with a
    # completed body.
    if rca.status == RcaStatus.PENDING.value:
        rca.status = RcaStatus.IN_PROGRESS.value
        rca.started_at = rca.started_at or _now()

    await session.flush()
    return rca


async def complete(session: AsyncSession, rca: Rca, *, user: User) -> Rca:
    """Mark the analysis done.

    Requires a root cause and a resolution and nothing else - the minimum
    that makes the record worth having later. Deliberately does **not** touch
    the incident: the two lifecycles are independent.
    """
    if rca.status == RcaStatus.COMPLETED.value:
        return rca
    if rca.status == RcaStatus.NOT_REQUIRED.value:
        raise RcaError("this RCA is marked not required")
    if not (rca.root_cause or "").strip():
        raise RcaError("a root cause is required to complete an RCA")
    if not (rca.resolution or "").strip():
        raise RcaError("a resolution is required to complete an RCA")

    rca.status = RcaStatus.COMPLETED.value
    rca.completed_at = _now()
    rca.completed_by = user.username
    await session.flush()

    logger.info(
        "rca_completed",
        rca_id=rca.id, incident_id=rca.incident_id,
        category=rca.root_cause_category, by=user.username,
    )
    return rca


async def mark_not_required(
    session: AsyncSession,
    incident: Incident,
    *,
    user: User,
    reason: str | None = None,
) -> Rca:
    """Record a deliberate decision not to analyse this one.

    Worth storing rather than leaving blank: "we looked and decided not to"
    is a different state from "nobody has looked", and only the first should
    disappear from the pending queue.
    """
    rca = await get_for_incident(session, incident.id)
    if rca is None:
        endpoint = incident.endpoint
        rca = Rca(
            incident_id=incident.id,
            endpoint_id=incident.endpoint_id,
            endpoint_name=getattr(endpoint, "name", None),
            application=getattr(endpoint, "application", None),
            environment=(
                endpoint.environment.name
                if getattr(endpoint, "environment", None)
                else None
            ),
        )
        session.add(rca)
    if rca.status == RcaStatus.COMPLETED.value:
        raise RcaError("this RCA is already complete")

    rca.status = RcaStatus.NOT_REQUIRED.value
    rca.not_required_reason = (reason or "").strip() or None
    rca.requested_by_id = rca.requested_by_id or user.id
    rca.requested_by = rca.requested_by or user.username
    await session.flush()
    return rca


async def generate_draft(
    session: AsyncSession, rca: Rca, incident: Incident
) -> dict[str, Any]:
    """Build a draft and a timeline from local data only."""
    evidence = await rca_draft.gather_evidence(session, incident)
    draft = rca_draft.build_draft(evidence)
    draft["timeline"] = rca_draft.build_timeline(evidence)
    draft["evidence"] = {
        "incident_started_at": incident.started_at,
        "incident_resolved_at": incident.resolved_at,
        "duration_seconds": incident.duration_seconds,
        "endpoint_name": getattr(evidence["endpoint"], "name", None),
        "http_codes": evidence["http_codes_during"],
        "failure_reasons": evidence["failure_reasons_during"],
        "first_error": evidence["first_error"],
        "failed_checks": evidence["failed_checks"],
        "baseline_response_time_ms": evidence["baseline_response_time_ms"],
        "similar_incidents_90d": evidence["similar_incidents"],
        "similar_same_reason_90d": evidence["similar_same_reason"],
        "preceding_change": (
            {
                "id": evidence["preceding_change"].id,
                "reference": evidence["preceding_change"].reference,
                "application": evidence["preceding_change"].application,
                "completed_at": evidence["preceding_change"].completed_at,
                "minutes_before_incident": evidence["change_gap_minutes"],
            }
            if evidence["preceding_change"]
            else None
        ),
        "diagnosis": (
            {
                "id": evidence["diagnosis"].id,
                "verdict": evidence["diagnosis"].verdict,
                "headline": evidence["diagnosis"].headline,
                "confidence": evidence["diagnosis"].confidence,
                "severity": evidence["diagnosis"].severity,
                "root_cause": evidence["diagnosis"].root_cause,
                "candidates": evidence["diagnosis"].candidates,
                "created_at": evidence["diagnosis"].created_at,
            }
            if evidence["diagnosis"] is not None
            else None
        ),
        "comment_count": len(evidence["comments"]),
    }
    return draft


# -------------------------------------------------------------- comments
async def add_comment(
    session: AsyncSession, incident: Incident, *, user: User, body: str
) -> IncidentComment:
    body = (body or "").strip()
    if not body:
        raise RcaError("a comment cannot be empty")
    if len(body) > 4000:
        raise RcaError("a comment must be at most 4000 characters")

    comment = IncidentComment(
        incident_id=incident.id,
        user_id=user.id,
        username=user.username,
        body=body,
    )
    session.add(comment)
    await session.flush()
    return comment


async def comments_for(
    session: AsyncSession, incident_id: int
) -> list[IncidentComment]:
    return list(
        (
            await session.execute(
                select(IncidentComment)
                .where(IncidentComment.incident_id == incident_id)
                .order_by(IncidentComment.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------- listing
def apply_filters(
    stmt: Select,
    *,
    search: str | None = None,
    statuses: Sequence[str] | None = None,
    applications: Sequence[str] | None = None,
    environments: Sequence[str] | None = None,
    categories: Sequence[str] | None = None,
    owner_user_id: Any | None = None,
    owner_team: str | None = None,
    mine_for: User | None = None,
    overdue_only: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Select:
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Rca.endpoint_name).like(needle),
                func.lower(Rca.application).like(needle),
                func.lower(Rca.root_cause).like(needle),
                func.lower(Rca.owner_user_name).like(needle),
                func.lower(Rca.owner_team).like(needle),
            )
        )
    if statuses:
        stmt = stmt.where(Rca.status.in_(list(statuses)))
    if applications:
        stmt = stmt.where(
            func.lower(Rca.application).in_([a.lower() for a in applications])
        )
    if environments:
        stmt = stmt.where(
            func.lower(Rca.environment).in_([e.lower() for e in environments])
        )
    if categories:
        stmt = stmt.where(Rca.root_cause_category.in_(list(categories)))
    if owner_user_id is not None:
        stmt = stmt.where(Rca.owner_user_id == owner_user_id)
    if owner_team:
        stmt = stmt.where(func.lower(Rca.owner_team) == owner_team.lower())

    # "Mine" spans both ownership kinds - a person and the team they are in.
    if mine_for is not None:
        clauses = [Rca.owner_user_id == mine_for.id]
        team = getattr(mine_for, "team", None)
        if team:
            clauses.append(func.lower(Rca.owner_team) == team.lower())
        stmt = stmt.where(or_(*clauses))

    if overdue_only:
        stmt = stmt.where(
            Rca.due_at.is_not(None),
            Rca.due_at < _now(),
            Rca.status.in_(list(OPEN_RCA_STATUSES)),
        )
    if since:
        stmt = stmt.where(Rca.created_at >= since)
    if until:
        stmt = stmt.where(Rca.created_at <= until)
    return stmt


# ------------------------------------------------------------- dashboard
async def dashboard(session: AsyncSession) -> dict[str, Any]:
    """Counts for the RCA overview, plus the pending queue."""
    status_rows = (
        await session.execute(
            select(Rca.status, func.count(Rca.id)).group_by(Rca.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in status_rows}

    total_incidents = int(
        (await session.execute(select(func.count(Incident.id)))).scalar() or 0
    )

    pending = list(
        (
            await session.execute(
                select(Rca)
                .where(Rca.status.in_(list(OPEN_RCA_STATUSES)))
                .order_by(Rca.due_at.asc().nulls_last(), Rca.created_at.asc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    overdue = int(
        (
            await session.execute(
                select(func.count(Rca.id)).where(
                    Rca.due_at.is_not(None),
                    Rca.due_at < _now(),
                    Rca.status.in_(list(OPEN_RCA_STATUSES)),
                )
            )
        ).scalar()
        or 0
    )

    # Incidents with no RCA record at all - the true backlog, and the number
    # that would otherwise stay invisible.
    unassessed = int(
        (
            await session.execute(
                select(func.count(Incident.id)).where(
                    Incident.id.notin_(select(Rca.incident_id))
                )
            )
        ).scalar()
        or 0
    )

    return {
        "total_incidents": total_incidents,
        "not_requested": unassessed,
        "pending": counts.get(RcaStatus.PENDING.value, 0),
        "in_progress": counts.get(RcaStatus.IN_PROGRESS.value, 0),
        "completed": counts.get(RcaStatus.COMPLETED.value, 0),
        "not_required": counts.get(RcaStatus.NOT_REQUIRED.value, 0),
        "overdue": overdue,
        "pending_queue": pending,
    }


# ------------------------------------------------------------- analytics
async def analytics(
    session: AsyncSession, *, window_days: int = ANALYTICS_WINDOW_DAYS
) -> dict[str, Any]:
    """Reports built only from stored RCA records.

    Every figure here comes from something an engineer actually wrote. None
    of it is inferred, so an empty section means "nobody has recorded this
    yet" rather than "nothing happened".
    """
    since = _now() - timedelta(days=window_days)

    completed = list(
        (
            await session.execute(
                select(Rca).where(
                    Rca.status == RcaStatus.COMPLETED.value,
                    Rca.created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    all_in_window = list(
        (
            await session.execute(select(Rca).where(Rca.created_at >= since))
        )
        .scalars()
        .all()
    )

    # ---- completion rate, excluding the ones nobody was asked to do
    eligible = [
        r for r in all_in_window if r.status != RcaStatus.NOT_REQUIRED.value
    ]
    completion_rate = (
        round(len(completed) / len(eligible) * 100, 1) if eligible else None
    )

    durations = [
        (r.completed_at - (r.requested_at or r.created_at)).total_seconds() / 86400
        for r in completed
        if r.completed_at and (r.requested_at or r.created_at)
    ]
    avg_days = round(sum(durations) / len(durations), 1) if durations else None

    categories = Counter(
        r.root_cause_category for r in completed if r.root_cause_category
    )
    category_total = sum(categories.values()) or 1
    top_categories = [
        {
            "category": category,
            "count": count,
            "percent": round(count / category_total * 100, 1),
        }
        for category, count in categories.most_common()
    ]

    owners = Counter(r.owner_label for r in all_in_window if r.owner_label)
    applications = Counter(r.application for r in all_in_window if r.application)

    deployment_related = sum(
        1
        for r in completed
        if r.change_id is not None
        or r.root_cause_category == RootCauseCategory.DEPLOYMENT.value
    )

    return {
        "window_days": window_days,
        "completed": len(completed),
        "eligible": len(eligible),
        "completion_rate_percent": completion_rate,
        "average_completion_days": avg_days,
        "top_root_causes": top_categories,
        "by_owner": [
            {"owner": owner, "count": count} for owner, count in owners.most_common(10)
        ],
        "by_application": [
            {"application": app, "count": count}
            for app, count in applications.most_common(10)
        ],
        "deployment_related": deployment_related,
        "deployment_related_percent": (
            round(deployment_related / len(completed) * 100, 1) if completed else None
        ),
    }


async def recurring_root_causes(
    session: AsyncSession, *, window_days: int = ANALYTICS_WINDOW_DAYS
) -> list[dict[str, Any]]:
    """Root causes that keep coming back.

    Grouped by category *and* by a normalised first line of the written cause,
    because "Database connection exhaustion" recorded five times by three
    people is the single most valuable pattern this data can surface - and it
    is invisible if you only count categories.
    """
    since = _now() - timedelta(days=window_days)
    rows = list(
        (
            await session.execute(
                select(Rca).where(
                    Rca.status == RcaStatus.COMPLETED.value,
                    Rca.created_at >= since,
                    Rca.root_cause.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[str, dict[str, Any]] = {}
    for rca in rows:
        headline = (rca.root_cause or "").strip().split("\n")[0][:160]
        key = headline.lower()
        if not key:
            continue
        bucket = grouped.setdefault(
            key,
            {
                "root_cause": headline,
                "category": rca.root_cause_category,
                "occurrences": 0,
                "applications": set(),
                "last_occurrence": None,
                "incident_ids": [],
            },
        )
        bucket["occurrences"] += 1
        if rca.application:
            bucket["applications"].add(rca.application)
        moment = rca.completed_at or rca.created_at
        if bucket["last_occurrence"] is None or moment > bucket["last_occurrence"]:
            bucket["last_occurrence"] = moment
        bucket["incident_ids"].append(rca.incident_id)

    results = [
        {
            **bucket,
            "applications": sorted(bucket["applications"]),
            "application_count": len(bucket["applications"]),
            "incident_ids": bucket["incident_ids"][:10],
        }
        for bucket in grouped.values()
        if bucket["occurrences"] >= 2
    ]
    return sorted(results, key=lambda item: item["occurrences"], reverse=True)[:10]


async def similar_past_rcas(
    session: AsyncSession, incident: Incident, *, limit: int = 3
) -> list[dict[str, Any]]:
    """Completed RCAs for comparable past incidents.

    Historical context only. The wording at every call site says so: a similar
    incident three weeks ago is a place to look first, not a diagnosis.
    """
    since = _now() - timedelta(days=180)
    stmt = (
        select(Rca)
        .join(Incident, Incident.id == Rca.incident_id)
        .where(
            Rca.status == RcaStatus.COMPLETED.value,
            Rca.incident_id != incident.id,
            Rca.created_at >= since,
        )
        .order_by(Rca.completed_at.desc())
        .limit(limit * 4)
    )

    # Same endpoint first, then the same application, then the same failure
    # reason anywhere - narrowest relevance first.
    candidates = list((await session.execute(stmt)).scalars().unique().all())
    endpoint_name = getattr(incident.endpoint, "name", None)
    application = getattr(incident.endpoint, "application", None)

    def relevance(rca: Rca) -> int:
        if rca.endpoint_id == incident.endpoint_id:
            return 0
        if application and rca.application == application:
            return 1
        return 2

    ordered = sorted(candidates, key=lambda r: (relevance(r), -(r.id or 0)))
    return [
        {
            "rca_id": rca.id,
            "incident_id": rca.incident_id,
            "endpoint_name": rca.endpoint_name,
            "application": rca.application,
            "root_cause": rca.root_cause,
            "root_cause_category": rca.root_cause_category,
            "resolution": rca.resolution,
            "completed_at": rca.completed_at,
            "same_endpoint": rca.endpoint_id == incident.endpoint_id,
            "days_ago": (
                (_now() - rca.completed_at).days if rca.completed_at else None
            ),
        }
        for rca in ordered[:limit]
    ]
