"""Endpoints, tags and environments."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AuthType, CheckType, EndpointStatus, SslStatus
from app.models.base import (
    Base,
    BigIntType,
    JSONType,
    TimestampMixin,
    TimestampTZ,
    UUIDPrimaryKeyMixin,
)

endpoint_tags = Table(
    "endpoint_tags",
    Base.metadata,
    Column(
        "endpoint_id",
        Uuid(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        Uuid(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Tag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    color: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(String(255))

    endpoints: Mapped[list["Endpoint"]] = relationship(
        secondary=endpoint_tags, back_populates="tags"
    )


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Environments are rows, not an enum, so teams can add their own."""

    __tablename__ = "environments"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(255))
    color: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    endpoints: Mapped[list["Endpoint"]] = relationship(back_populates="environment")


class Endpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        # The scheduler's hot query: due, enabled endpoints ordered by due time.
        Index(
            "ix_endpoints_due",
            "monitoring_enabled",
            "is_paused",
            "next_check_at",
        ),
        Index("ix_endpoints_status_env", "current_status", "environment_id"),
        Index("ix_endpoints_ssl_expiry", "ssl_expires_at"),
    )

    # ---------------------------------------------------------- identity
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Parsed once at write time so the SSL page and filters can query on host
    # without re-parsing 1000s of URLs per request.
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, default="https")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, default="/")

    check_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CheckType.HTTP.value
    )
    http_method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")

    # ------------------------------------------------------ organisation
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("environments.id", ondelete="SET NULL"),
        index=True,
    )
    environment: Mapped[Environment | None] = relationship(
        back_populates="endpoints", lazy="joined"
    )
    tags: Mapped[list[Tag]] = relationship(
        secondary=endpoint_tags,
        back_populates="endpoints",
        lazy="selectin",
        order_by=Tag.name,
    )

    description: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(128), index=True)
    team: Mapped[str | None] = mapped_column(String(128), index=True)
    application: Mapped[str | None] = mapped_column(String(128), index=True)

    # ---------------------------------------------------- check settings
    monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why monitoring is paused, and which change owns the pause. A deployment
    # pause must only ever be lifted by the change that applied it, so an
    # operator's own manual pause is never silently undone.
    pause_reason: Mapped[str | None] = mapped_column(String(255))
    paused_by_change_id: Mapped[int | None] = mapped_column(
        BigIntType, ForeignKey("changes.id", ondelete="SET NULL"), index=True
    )
    # A health path discovered automatically after the configured one turned
    # out to be absent. Kept separate from `url` so the operator's own
    # configuration is never rewritten behind their back.
    resolved_health_path: Mapped[str | None] = mapped_column(String(255))
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    # Comma-separated list, e.g. "200" or "200,204,301".
    expected_status_codes: Mapped[str] = mapped_column(
        String(128), nullable=False, default="200"
    )
    expected_body_substring: Mapped[str | None] = mapped_column(String(255))
    follow_redirects: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    verify_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ssl_monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    request_body: Mapped[str | None] = mapped_column(Text)

    # Non-sensitive headers only. Authentication material lives encrypted in
    # ``auth_secret_encrypted`` and is never serialised back to a client.
    custom_headers: Mapped[dict | None] = mapped_column(JSONType)
    auth_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AuthType.NONE.value
    )
    auth_username: Mapped[str | None] = mapped_column(String(128))
    auth_header_name: Mapped[str | None] = mapped_column(String(128))
    auth_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    auth_secret_hint: Mapped[str | None] = mapped_column(String(64))

    # -------------------------------------------------- alert thresholds
    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    response_time_threshold_ms: Mapped[int | None] = mapped_column(Integer)
    ssl_warning_days: Mapped[int | None] = mapped_column(Integer)
    ssl_critical_days: Mapped[int | None] = mapped_column(Integer)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ------------------------------------------------------ live state
    current_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EndpointStatus.UNKNOWN.value, index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_response_time_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    consecutive_successes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    total_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ssl_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SslStatus.UNABLE_TO_CHECK.value
    )
    ssl_expires_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    ssl_days_remaining: Mapped[int | None] = mapped_column(Integer)
    ssl_issuer: Mapped[str | None] = mapped_column(String(255))
    ssl_common_name: Mapped[str | None] = mapped_column(String(255))

    # ------------------------------------------------------- scheduling
    next_check_at: Mapped[datetime | None] = mapped_column(TimestampTZ, index=True)
    # Set while a worker holds the row, so a crashed worker's endpoint becomes
    # claimable again after the lease expires.
    lease_expires_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    leased_by: Mapped[str | None] = mapped_column(String(64))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    results: Mapped[list["MonitoringResult"]] = relationship(  # noqa: F821
        back_populates="endpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    certificates: Mapped[list["SslCertificate"]] = relationship(  # noqa: F821
        back_populates="endpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    incidents: Mapped[list["Incident"]] = relationship(  # noqa: F821
        back_populates="endpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ---------------------------------------------------------- helpers
    @property
    def is_https(self) -> bool:
        return self.protocol == "https"

    @property
    def expected_status_list(self) -> list[int]:
        codes: list[int] = []
        for chunk in (self.expected_status_codes or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                codes.append(int(chunk))
            except ValueError:
                continue
        return codes or [200]

    @property
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]

    @property
    def uptime_ratio(self) -> float | None:
        if not self.total_checks:
            return None
        return (self.total_checks - self.total_failures) / self.total_checks
