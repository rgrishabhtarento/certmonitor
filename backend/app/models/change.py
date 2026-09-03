"""Change management: requests, comments and activity.

Scope is deliberately small - approval before production, a record of who
deployed what and when, and automatic coordination with endpoint monitoring.
Anything resembling a full ITSM workflow is intentionally absent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ChangeRisk, ChangeStatus
from app.models.base import (
    Base,
    BigIntType,
    JSONType,
    TimestampTZ,
    utcnow,
)

# Which endpoints a change touches, plus the one fact needed to undo the
# pause correctly: whether the endpoint was ALREADY paused beforehand. Without
# it, completing a deployment would silently resume monitoring an operator had
# deliberately turned off.
change_endpoints = Table(
    "change_endpoints",
    Base.metadata,
    Column(
        "change_id",
        BigIntType,
        ForeignKey("changes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "endpoint_id",
        Uuid(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("was_paused_before", Boolean, nullable=False, server_default="false"),
)


class Change(Base):
    __tablename__ = "changes"
    __table_args__ = (
        Index("ix_changes_status", "status"),
        Index("ix_changes_expected_start", "expected_start_at"),
        Index("ix_changes_app_env", "application", "environment_id"),
        Index("ix_changes_requester", "requester_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)

    # Human-facing identifier, e.g. CHG-2026-0001.
    reference: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    application: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("environments.id", ondelete="SET NULL"),
        index=True,
    )
    environment: Mapped["Environment | None"] = relationship(  # noqa: F821
        lazy="joined"
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    expected_start_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
    expected_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    risk: Mapped[str] = mapped_column(
        String(8), nullable=False, default=ChangeRisk.LOW.value
    )

    rollback_plan: Mapped[str | None] = mapped_column(Text)
    deployment_notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ChangeStatus.DRAFT.value
    )

    # ------------------------------------------------------------ people
    requester_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Denormalised so the record still reads correctly after a user is deleted.
    requester_name: Mapped[str | None] = mapped_column(String(64))

    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approver_name: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Captured from the authenticated session at deployment start and never
    # writable through the API - "who deployed this" has to be trustworthy.
    deployer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    deployer_name: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    completed_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # Result of the health check run when monitoring resumed.
    health_check: Mapped[list | None] = mapped_column(JSONType)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    endpoints: Mapped[list["Endpoint"]] = relationship(  # noqa: F821
        secondary=change_endpoints, lazy="selectin"
    )
    comments: Mapped[list["ChangeComment"]] = relationship(
        back_populates="change",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChangeComment.created_at",
    )
    activity: Mapped[list["ChangeActivity"]] = relationship(
        back_populates="change",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChangeActivity.created_at",
    )

    # ----------------------------------------------------------- helpers
    @property
    def is_open(self) -> bool:
        from app.core.enums import TERMINAL_CHANGE_STATUSES

        return self.status not in TERMINAL_CHANGE_STATUSES

    @property
    def is_deploying(self) -> bool:
        return self.status == ChangeStatus.DEPLOYMENT_IN_PROGRESS.value

    @property
    def environment_name(self) -> str | None:
        return self.environment.name if self.environment else None

    @property
    def actual_duration_minutes(self) -> int | None:
        if not self.started_at or not self.completed_at:
            return None
        return max(0, int((self.completed_at - self.started_at).total_seconds() // 60))


class ChangeComment(Base):
    """A comment on a change.

    Anyone who can see the change can post one - the timeline is the shared
    record of what happened during a deployment.
    """

    __tablename__ = "change_comments"
    __table_args__ = (Index("ix_change_comments_change", "change_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(
        BigIntType, ForeignKey("changes.id", ondelete="CASCADE"), nullable=False
    )
    change: Mapped[Change] = relationship(back_populates="comments", lazy="noload")

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    username: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )


class ChangeActivity(Base):
    """Append-only timeline entry.

    Separate from comments so the automatic events (monitoring paused, health
    check ran) stay distinguishable from what people wrote.
    """

    __tablename__ = "change_activity"
    __table_args__ = (Index("ix_change_activity_change", "change_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    change_id: Mapped[int] = mapped_column(
        BigIntType, ForeignKey("changes.id", ondelete="CASCADE"), nullable=False
    )
    change: Mapped[Change] = relationship(back_populates="activity", lazy="noload")

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )
