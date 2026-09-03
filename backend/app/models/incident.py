"""Incidents: one row per continuous outage, not per failed check."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import IncidentStatus, Severity
from app.models.base import Base, BigIntType, JSONType, TimestampTZ, utcnow


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_endpoint_status", "endpoint_id", "status"),
        Index("ix_incidents_started_at", "started_at"),
        # At most one open incident per endpoint. This is the database-level
        # guarantee behind "four failed checks are one incident".
        Index(
            "uq_incidents_one_open_per_endpoint",
            "endpoint_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped["Endpoint"] = relationship(  # noqa: F821
        back_populates="incidents", lazy="joined"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IncidentStatus.OPEN.value, index=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Severity.CRITICAL.value
    )

    started_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    reason: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    first_failure_status_code: Mapped[int | None] = mapped_column(Integer)

    failed_check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recovery_status_code: Mapped[int | None] = mapped_column(Integer)
    recovery_response_time_ms: Mapped[float | None] = mapped_column(Float)

    # Free-form timeline entries: {at, kind, detail}
    timeline: Mapped[list | None] = mapped_column(JSONType)
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )

    @property
    def is_open(self) -> bool:
        return self.status == IncidentStatus.OPEN.value
