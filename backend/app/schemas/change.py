"""Change management schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import ChangeRisk, ChangeStatus


class ChangeEndpointRef(BaseModel):
    """Flattened endpoint reference carried on a change."""

    id: uuid.UUID
    name: str
    url: str
    environment: str | None = None
    current_status: str | None = None
    is_paused: bool = False
    pause_reason: str | None = None


class ChangeCommentRead(BaseModel):
    id: int
    username: str | None = None
    body: str
    created_at: datetime


class ChangeActivityRead(BaseModel):
    id: int
    username: str | None = None
    action: str
    detail: str | None = None
    created_at: datetime


class ChangeListItem(BaseModel):
    id: int
    reference: str
    title: str
    application: str
    environment: str | None = None
    status: str
    risk: str
    expected_start_at: datetime
    expected_duration_minutes: int
    requester_name: str | None = None
    approver_name: str | None = None
    deployer_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    endpoint_count: int = 0
    created_at: datetime


class ChangeRead(ChangeListItem):
    description: str
    rollback_plan: str | None = None
    deployment_notes: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    failure_reason: str | None = None
    actual_duration_minutes: int | None = None
    health_check: list[dict[str, Any]] | None = None

    endpoints: list[ChangeEndpointRef] = Field(default_factory=list)
    comments: list[ChangeCommentRead] = Field(default_factory=list)
    activity: list[ChangeActivityRead] = Field(default_factory=list)

    # Server-computed so the UI never has to re-derive the workflow rules.
    requires_approval: bool = False
    can_edit: bool = False
    can_submit: bool = False
    can_approve: bool = False
    can_deploy: bool = False
    can_finish: bool = False
    can_cancel: bool = False
    can_comment: bool = False


class ChangeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    application: str = Field(min_length=1, max_length=128)
    environment: str | None = Field(
        default=None, description="Environment name or id."
    )
    description: str = Field(min_length=1)
    expected_start_at: datetime
    expected_duration_minutes: int = Field(default=30, ge=1, le=1440)
    risk: str = Field(default=ChangeRisk.LOW.value)

    endpoint_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=200,
        description="Endpoints whose monitoring is paused while this deploys.",
    )
    rollback_plan: str | None = None
    deployment_notes: str | None = None

    @field_validator("risk")
    @classmethod
    def _valid_risk(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {r.value for r in ChangeRisk}:
            raise ValueError("risk must be low, medium or high")
        return value


class ChangeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    application: str | None = Field(default=None, min_length=1, max_length=128)
    environment: str | None = None
    description: str | None = Field(default=None, min_length=1)
    expected_start_at: datetime | None = None
    expected_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    risk: str | None = None
    endpoint_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    rollback_plan: str | None = None
    deployment_notes: str | None = None

    @field_validator("risk")
    @classmethod
    def _valid_risk(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in {r.value for r in ChangeRisk}:
            raise ValueError("risk must be low, medium or high")
        return value


class ApprovalRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class RejectionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class CompleteDeploymentRequest(BaseModel):
    deployment_notes: str | None = Field(default=None, max_length=4000)


class FailDeploymentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    deployment_notes: str | None = Field(default=None, max_length=4000)


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class DeploymentResult(BaseModel):
    """What happened to monitoring as a result of a deployment transition."""

    change: ChangeRead
    monitoring_paused: list[dict[str, Any]] = Field(default_factory=list)
    monitoring_resumed: list[dict[str, Any]] = Field(default_factory=list)
    health_check: list[dict[str, Any]] = Field(default_factory=list)


class ChangeDashboard(BaseModel):
    pending_approval: int = 0
    approved: int = 0
    active_deployments: int = 0
    completed_today: int = 0
    failed_today: int = 0
    draft: int = 0
    max_pause_minutes: int = 240

    upcoming: list[ChangeListItem] = Field(default_factory=list)
    active: list[ChangeListItem] = Field(default_factory=list)
    # Deployments running longer than max_pause_minutes - their endpoints are
    # still silenced, which is worth surfacing loudly.
    overrunning: list[ChangeListItem] = Field(default_factory=list)


class ChangeOptions(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: [s.value for s in ChangeStatus])
    risks: list[str] = Field(default_factory=lambda: [r.value for r in ChangeRisk])
    applications: list[str] = Field(default_factory=list)
    approval_environments: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- builders
def endpoint_ref(endpoint: Any) -> ChangeEndpointRef:
    return ChangeEndpointRef(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.url,
        environment=endpoint.environment.name if endpoint.environment else None,
        current_status=endpoint.current_status,
        is_paused=endpoint.is_paused,
        pause_reason=endpoint.pause_reason,
    )


def to_list_item(change: Any) -> ChangeListItem:
    return ChangeListItem(
        id=change.id,
        reference=change.reference,
        title=change.title,
        application=change.application,
        environment=change.environment_name,
        status=change.status,
        risk=change.risk,
        expected_start_at=change.expected_start_at,
        expected_duration_minutes=change.expected_duration_minutes,
        requester_name=change.requester_name,
        approver_name=change.approver_name,
        deployer_name=change.deployer_name,
        started_at=change.started_at,
        completed_at=change.completed_at,
        endpoint_count=len(change.endpoints or []),
        created_at=change.created_at,
    )


def to_read(
    change: Any,
    *,
    permissions: dict[str, bool] | None = None,
    include_timeline: bool = True,
) -> ChangeRead:
    """Build the detail view.

    Constructed field by field rather than with ``model_validate``: the ORM
    row's ``endpoints`` are full objects whose ``environment`` is itself an
    object, so letting Pydantic coerce them into the flattened reference would
    fail.
    """
    permissions = permissions or {}
    return ChangeRead(
        **to_list_item(change).model_dump(),
        description=change.description,
        rollback_plan=change.rollback_plan,
        deployment_notes=change.deployment_notes,
        approved_at=change.approved_at,
        rejection_reason=change.rejection_reason,
        failure_reason=change.failure_reason,
        actual_duration_minutes=change.actual_duration_minutes,
        health_check=change.health_check,
        endpoints=[endpoint_ref(e) for e in (change.endpoints or [])],
        comments=[
            ChangeCommentRead(
                id=c.id, username=c.username, body=c.body, created_at=c.created_at
            )
            for c in (change.comments or [])
        ]
        if include_timeline
        else [],
        activity=[
            ChangeActivityRead(
                id=a.id, username=a.username, action=a.action,
                detail=a.detail, created_at=a.created_at,
            )
            for a in (change.activity or [])
        ]
        if include_timeline
        else [],
        **permissions,
    )
