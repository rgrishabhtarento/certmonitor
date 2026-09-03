"""Audit log and runtime-configurable system settings."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntType, JSONType, TimestampTZ, utcnow


class AuditLog(Base):
    """Append-only record of administrative actions.

    ``username`` is denormalised so the trail survives user deletion.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_user_action", "user_id", "action"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True, autoincrement=True)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    username: Mapped[str | None] = mapped_column(String(64), index=True)

    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    resource_name: Mapped[str | None] = mapped_column(String(255))

    # Never contains credentials: writers pass through a scrubbing helper.
    details: Mapped[dict | None] = mapped_column(JSONType)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_method: Mapped[str | None] = mapped_column(String(10))
    request_path: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, server_default=func.now()
    )


class SystemSetting(Base):
    """Key/value settings editable from the UI.

    Environment variables provide the boot defaults; a row here overrides them
    at runtime so operators can retune thresholds without a redeploy.
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, default="string")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general", index=True
    )
    label: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    # JSON array of allowed values, rendered as a dropdown in the UI.
    allowed_values: Mapped[list | None] = mapped_column(JSONType)
    min_value: Mapped[float | None] = mapped_column()
    max_value: Mapped[float | None] = mapped_column()
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
