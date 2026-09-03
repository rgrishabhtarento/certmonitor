"""Stored diagnoses.

A single diagnosis answers "what is wrong right now". Keeping them lets the
engine answer a harder and more useful question: *is this the fourth time this
month?* A recurring 502 every Monday morning is a capacity problem, not four
unrelated outages, and only the history makes that visible.

Only the conclusion is stored, not the full probe payload - the raw layer data
is large, loses relevance within minutes, and would grow this table without
bound.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntType, JSONType, TimestampTZ, utcnow


class Diagnosis(Base):
    __tablename__ = "diagnoses"
    __table_args__ = (
        Index("ix_diagnoses_endpoint_time", "endpoint_id", "created_at"),
        Index("ix_diagnoses_verdict", "verdict"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped["Endpoint"] = relationship(lazy="noload")  # noqa: F821

    # Denormalised so the record still reads after the endpoint is renamed or
    # the user is deleted.
    endpoint_name: Mapped[str | None] = mapped_column(String(160))
    application: Mapped[str | None] = mapped_column(String(128), index=True)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    requested_by: Mapped[str | None] = mapped_column(String(64))
    focus: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")

    # ------------------------------------------------------ conclusion
    verdict: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text)

    endpoint_status: Mapped[str | None] = mapped_column(String(16))
    deepest_layer_ok: Mapped[str | None] = mapped_column(String(8))
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    response_time_ms: Mapped[float | None] = mapped_column(Float)

    # Ranked candidate causes, as {cause, label, confidence, score, why}.
    candidates: Mapped[list | None] = mapped_column(JSONType)
    # Recommended actions, as {step, title, detail, risk, command}.
    actions: Mapped[list | None] = mapped_column(JSONType)

    # Cross-links to whatever the diagnosis correlated with.
    incident_id: Mapped[int | None] = mapped_column(BigIntType)
    change_id: Mapped[int | None] = mapped_column(BigIntType)

    # Filled in later by an operator: what actually fixed it. This is the
    # field that turns a pile of diagnoses into institutional knowledge.
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    resolved_by: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ,
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
