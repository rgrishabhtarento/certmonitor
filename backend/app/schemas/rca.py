"""RCA and operational-intelligence schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import RcaOwnerType, RcaStatus, RootCauseCategory


class PreventiveAction(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    done: bool = False


class TimelineEntry(BaseModel):
    at: datetime | None = None
    kind: str = Field(default="note", max_length=32)
    detail: str = Field(min_length=1, max_length=1000)
    # Where it came from: monitoring, change, diagnosis, comment, incident, or
    # manual. Rendered distinctly so a derived fact is never confused with
    # something a person typed.
    source: str = Field(default="manual", max_length=16)


class IncidentCommentRead(BaseModel):
    id: int
    username: str | None = None
    body: str
    created_at: datetime


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class RcaListItem(BaseModel):
    id: int
    incident_id: int
    endpoint_id: uuid.UUID | None = None
    endpoint_name: str | None = None
    application: str | None = None
    environment: str | None = None

    status: str
    owner_type: str | None = None
    owner_user_name: str | None = None
    owner_team: str | None = None
    owner_label: str | None = None

    root_cause_category: str | None = None
    requested_by: str | None = None
    requested_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    is_overdue: bool = False
    age_days: int | None = None
    created_at: datetime


class RcaRead(RcaListItem):
    root_cause: str | None = None
    impact: str | None = None
    resolution: str | None = None
    preventive_actions: list[PreventiveAction] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    not_required_reason: str | None = None
    started_at: datetime | None = None
    completed_by: str | None = None
    diagnosis_id: int | None = None
    change_id: int | None = None

    # Server-computed so the UI never re-derives the rules.
    can_edit: bool = False
    can_assign: bool = False
    can_complete: bool = False

    incident: dict[str, Any] | None = None
    comments: list[IncidentCommentRead] = Field(default_factory=list)
    similar_past: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Completed RCAs for comparable past incidents. Historical "
        "context only - never evidence about this one.",
    )


class RcaRequest(BaseModel):
    owner_type: str | None = None
    owner_user_id: uuid.UUID | None = None
    owner_team: str | None = Field(default=None, max_length=64)
    due_in_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("owner_type")
    @classmethod
    def _valid_owner_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in {o.value for o in RcaOwnerType}:
            raise ValueError("owner_type must be 'individual' or 'team'")
        return value


class RcaAssign(BaseModel):
    owner_type: str
    owner_user_id: uuid.UUID | None = None
    owner_team: str | None = Field(default=None, max_length=64)
    due_in_days: int | None = Field(default=None, ge=0, le=365)

    @field_validator("owner_type")
    @classmethod
    def _valid_owner_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {o.value for o in RcaOwnerType}:
            raise ValueError("owner_type must be 'individual' or 'team'")
        return value


class RcaUpdate(BaseModel):
    root_cause: str | None = None
    root_cause_category: str | None = None
    impact: str | None = None
    resolution: str | None = None
    preventive_actions: list[PreventiveAction] | None = None
    timeline: list[TimelineEntry] | None = None
    due_at: datetime | None = None

    @field_validator("root_cause_category")
    @classmethod
    def _valid_category(cls, value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip().lower()
        if value not in {c.value for c in RootCauseCategory}:
            raise ValueError("unknown root cause category")
        return value


class NotRequiredRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class RcaDraft(BaseModel):
    """A locally generated starting point, for the owner to edit.

    Assembled from the incident, the diagnosis run at the time, the deployment
    that preceded it, monitoring history and the incident comments. Every
    field is traceable to a stored record, and where the data does not support
    a statement it says "Not available from monitoring data." rather than
    filling the gap.
    """

    root_cause: str | None = None
    root_cause_category: str | None = None
    impact: str | None = None
    resolution: str | None = None
    preventive_actions: list[PreventiveAction] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    diagnosis_id: int | None = None
    change_id: int | None = None
    notice: str


class RcaDashboard(BaseModel):
    total_incidents: int = 0
    not_requested: int = 0
    pending: int = 0
    in_progress: int = 0
    completed: int = 0
    not_required: int = 0
    overdue: int = 0
    pending_queue: list[RcaListItem] = Field(default_factory=list)


class RcaAnalytics(BaseModel):
    window_days: int
    completed: int = 0
    eligible: int = 0
    completion_rate_percent: float | None = None
    average_completion_days: float | None = None
    top_root_causes: list[dict[str, Any]] = Field(default_factory=list)
    by_owner: list[dict[str, Any]] = Field(default_factory=list)
    by_application: list[dict[str, Any]] = Field(default_factory=list)
    deployment_related: int = 0
    deployment_related_percent: float | None = None
    recurring_root_causes: list[dict[str, Any]] = Field(default_factory=list)


class RcaOptions(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: [s.value for s in RcaStatus])
    categories: list[str] = Field(
        default_factory=lambda: [c.value for c in RootCauseCategory]
    )
    teams: list[str] = Field(default_factory=list)
    applications: list[str] = Field(default_factory=list)


# ------------------------------------------------------- builders
def to_list_item(rca: Any) -> RcaListItem:
    return RcaListItem(
        id=rca.id,
        incident_id=rca.incident_id,
        endpoint_id=rca.endpoint_id,
        endpoint_name=rca.endpoint_name,
        application=rca.application,
        environment=rca.environment,
        status=rca.status,
        owner_type=rca.owner_type,
        owner_user_name=rca.owner_user_name,
        owner_team=rca.owner_team,
        owner_label=rca.owner_label,
        root_cause_category=rca.root_cause_category,
        requested_by=rca.requested_by,
        requested_at=rca.requested_at,
        due_at=rca.due_at,
        completed_at=rca.completed_at,
        is_overdue=rca.is_overdue,
        age_days=rca.age_days,
        created_at=rca.created_at,
    )


def to_read(
    rca: Any,
    *,
    permissions: dict[str, bool] | None = None,
    incident: dict[str, Any] | None = None,
    comments: list[Any] | None = None,
    similar_past: list[dict[str, Any]] | None = None,
) -> RcaRead:
    permissions = permissions or {}
    return RcaRead(
        **to_list_item(rca).model_dump(),
        root_cause=rca.root_cause,
        impact=rca.impact,
        resolution=rca.resolution,
        preventive_actions=[
            PreventiveAction(**item) for item in (rca.preventive_actions or [])
        ],
        timeline=[TimelineEntry(**item) for item in (rca.timeline or [])],
        not_required_reason=rca.not_required_reason,
        started_at=rca.started_at,
        completed_by=rca.completed_by,
        diagnosis_id=rca.diagnosis_id,
        change_id=rca.change_id,
        incident=incident,
        comments=[
            IncidentCommentRead(
                id=c.id, username=c.username, body=c.body, created_at=c.created_at
            )
            for c in (comments or [])
        ],
        similar_past=similar_past or [],
        **permissions,
    )
