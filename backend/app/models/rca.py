"""Root-cause analysis, and incident comments.

Two design decisions shape this module.

**RCA is optional and independent.** It has its own lifecycle and never gates
the incident's. An incident can be resolved and closed while its RCA sits at
Pending, and completing an RCA changes nothing about the incident. A process
that blocks recovery on paperwork is a process people route around.

**Teams are the labels that already exist.** Ownership is a user *or* a team
name, where team names are the same free-text labels used on endpoints and now
on users. A team table plus a membership screen would be a migration and two
more pages without answering a single question the string cannot.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import RcaStatus
from app.models.base import Base, BigIntType, JSONType, TimestampTZ, utcnow


class Rca(Base):
    """One root-cause analysis, attached to one incident."""

    __tablename__ = "rcas"
    __table_args__ = (
        Index("ix_rcas_status_created", "status", "created_at"),
        Index("ix_rcas_owner_user", "owner_user_id"),
        Index("ix_rcas_owner_team", "owner_team"),
        Index("ix_rcas_category", "root_cause_category"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)

    # One RCA per incident. Enforced by the unique constraint rather than by
    # application code, so a double-click cannot create two.
    incident_id: Mapped[int] = mapped_column(
        BigIntType,
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    incident: Mapped["Incident"] = relationship(lazy="noload")  # noqa: F821

    # Denormalised context, so the RCA list needs no joins and still reads
    # correctly after an endpoint is renamed or deleted.
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("endpoints.id", ondelete="SET NULL")
    )
    endpoint_name: Mapped[str | None] = mapped_column(String(160))
    application: Mapped[str | None] = mapped_column(String(128), index=True)
    environment: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RcaStatus.PENDING.value, index=True
    )

    # ------------------------------------------------------- ownership
    owner_type: Mapped[str | None] = mapped_column(String(12))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    owner_user_name: Mapped[str | None] = mapped_column(String(64))
    owner_team: Mapped[str | None] = mapped_column(String(64))

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    requested_by: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    due_at: Mapped[datetime | None] = mapped_column(TimestampTZ)

    # -------------------------------------------------------- content
    root_cause: Mapped[str | None] = mapped_column(Text)
    root_cause_category: Mapped[str | None] = mapped_column(String(24))
    impact: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)

    # [{text, done}] - several small preventive actions beat one paragraph
    # nobody can tick off.
    preventive_actions: Mapped[list | None] = mapped_column(JSONType)

    # [{at, kind, detail, source}] - seeded from real events, then editable.
    timeline: Mapped[list | None] = mapped_column(JSONType)

    # What the diagnosis engine concluded at the time, kept so the RCA still
    # shows its evidence after the endpoint has long since recovered.
    diagnosis_id: Mapped[int | None] = mapped_column(BigIntType)
    change_id: Mapped[int | None] = mapped_column(BigIntType)

    started_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    completed_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    completed_by: Mapped[str | None] = mapped_column(String(64))
    not_required_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    # ----------------------------------------------------------- helpers
    @property
    def is_open(self) -> bool:
        return self.status in (RcaStatus.PENDING.value, RcaStatus.IN_PROGRESS.value)

    @property
    def owner_label(self) -> str | None:
        if self.owner_team:
            return self.owner_team
        return self.owner_user_name

    @property
    def is_overdue(self) -> bool:
        """Only meaningful when a due date was set - otherwise never overdue.

        RCA is optional, so an RCA without a deadline is not late; it simply
        has no deadline.
        """
        if not self.due_at or not self.is_open:
            return False
        return self.due_at < utcnow()

    @property
    def age_days(self) -> int | None:
        start = self.requested_at or self.created_at
        if not start:
            return None
        end = self.completed_at or utcnow()
        return max(0, (end - start).days)


class IncidentComment(Base):
    """A comment on an incident.

    The investigation happens in conversation - "started right after the
    deploy", "rollback done", "connections were exhausted" - and that
    conversation is the raw material of the RCA. Keeping it on the incident
    rather than in chat means the RCA owner inherits it instead of
    reconstructing it.
    """

    __tablename__ = "incident_comments"
    __table_args__ = (
        Index("ix_incident_comments_incident", "incident_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        BigIntType,
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    username: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )
