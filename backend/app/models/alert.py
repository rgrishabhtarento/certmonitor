"""Alerts and notification channels."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import NotificationChannelType, Severity
from app.models.base import (
    Base,
    BigIntType,
    JSONType,
    TimestampMixin,
    TimestampTZ,
    UUIDPrimaryKeyMixin,
    utcnow,
)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_created_at", "created_at"),
        Index("ix_alerts_endpoint_type", "endpoint_id", "alert_type"),
        Index("ix_alerts_ack", "is_acknowledged", "severity"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)

    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped["Endpoint | None"] = relationship(lazy="joined")  # noqa: F821
    incident_id: Mapped[int | None] = mapped_column(
        BigIntType, ForeignKey("incidents.id", ondelete="SET NULL")
    )

    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Severity.WARNING.value
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONType)

    is_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(TimestampTZ)

    # Notification bookkeeping: an alert row is created even when delivery
    # fails, so the UI still shows it and the failure is visible.
    notification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    notification_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    notification_error: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(TimestampTZ)

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )


class NotificationChannel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Delivery target for alerts.

    ``config_encrypted`` holds the whole provider config (webhook URL, SMTP
    password, routing key) as one encrypted blob so no secret is ever stored or
    returned in the clear. ``config_public`` holds only the non-sensitive parts
    that are safe to display back in the UI.
    """

    __tablename__ = "notification_channels"

    name: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    channel_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default=NotificationChannelType.WEBHOOK.value
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    config_encrypted: Mapped[str | None] = mapped_column(Text)
    config_public: Mapped[dict | None] = mapped_column(JSONType)

    min_severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Severity.WARNING.value
    )
    # Empty/NULL means "all event types".
    event_types: Mapped[list | None] = mapped_column(JSONType)
    # Empty/NULL means "all environments".
    environment_filter: Mapped[list | None] = mapped_column(JSONType)
    tag_filter: Mapped[list | None] = mapped_column(JSONType)

    last_used_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    last_error: Mapped[str | None] = mapped_column(Text)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
