"""Dashboard, statistics, settings and import/export schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.monitoring import IncidentRead


# ------------------------------------------------------------- dashboard
class SummaryCards(BaseModel):
    window: str
    since: datetime
    until: datetime

    total_endpoints: int = 0
    healthy: int = 0
    down: int = 0
    degraded: int = 0
    unknown: int = 0
    paused: int = 0

    ssl_certificates: int = 0
    ssl_valid: int = 0
    ssl_expiring_soon: int = 0
    ssl_critical: int = 0
    ssl_expired: int = 0
    ssl_invalid: int = 0
    ssl_unable_to_check: int = 0
    ssl_alerts: int = 0

    average_response_time_ms: float | None = None
    overall_uptime_percent: float | None = None
    total_checks: int = 0
    failed_checks: int = 0
    open_incidents: int = 0

    status_distribution: list[dict[str, Any]] = Field(default_factory=list)


class TimeSeriesPoint(BaseModel):
    timestamp: datetime
    checks: int = 0
    avg_response_time_ms: float | None = None
    min_response_time_ms: float | None = None
    max_response_time_ms: float | None = None
    failed_checks: int = 0
    degraded_checks: int = 0
    uptime_percent: float | None = None
    avg_dns_time_ms: float | None = None
    avg_connect_time_ms: float | None = None
    avg_tls_time_ms: float | None = None


class GroupAvailability(BaseModel):
    id: str | None = None
    name: str
    total: int = 0
    healthy: int = 0
    down: int = 0
    degraded: int = 0
    avg_response_time_ms: float | None = None
    health_percent: float | None = None
    uptime_percent: float | None = None


class ExpiryBucket(BaseModel):
    bucket: str
    count: int


class RankedEndpoint(BaseModel):
    endpoint_id: str
    name: str
    url: str
    failed_checks: int | None = None
    avg_response_time_ms: float | None = None
    checks: int | None = None


class DashboardResponse(BaseModel):
    """Everything the dashboard renders, in a single request.

    One round trip keeps the initial paint fast and the numbers internally
    consistent - separate requests could otherwise straddle a check cycle and
    disagree with each other.
    """

    generated_at: datetime
    summary: SummaryCards
    response_time_series: list[TimeSeriesPoint] = Field(default_factory=list)
    availability_by_environment: list[GroupAvailability] = Field(default_factory=list)
    availability_by_tag: list[GroupAvailability] = Field(default_factory=list)
    availability_by_team: list[GroupAvailability] = Field(default_factory=list)
    ssl_expiry_timeline: list[ExpiryBucket] = Field(default_factory=list)
    top_failing_endpoints: list[RankedEndpoint] = Field(default_factory=list)
    slowest_endpoints: list[RankedEndpoint] = Field(default_factory=list)
    open_incidents: list[IncidentRead] = Field(default_factory=list)
    recent_incidents: list[IncidentRead] = Field(default_factory=list)
    sla_target: float | None = None
    sla_breaches: list[GroupAvailability] = Field(default_factory=list)


# ------------------------------------------------------- endpoint detail
class WindowStats(BaseModel):
    window: str
    since: datetime
    until: datetime
    total_checks: int = 0
    failed_checks: int = 0
    degraded_checks: int = 0
    uptime_percent: float | None = None
    downtime_seconds: int = 0
    incident_count: int = 0
    avg_response_time_ms: float | None = None
    min_response_time_ms: float | None = None
    max_response_time_ms: float | None = None
    p95_response_time_ms: float | None = None


class EndpointStatsResponse(BaseModel):
    endpoint_id: uuid.UUID
    generated_at: datetime
    windows: dict[str, WindowStats]
    series: list[TimeSeriesPoint] = Field(default_factory=list)
    bucket_seconds: int


# --------------------------------------------------------------- settings
class SettingRead(BaseModel):
    key: str
    value: Any
    value_type: str
    category: str
    label: str | None = None
    description: str | None = None
    allowed_values: list[Any] | None = None
    min_value: float | None = None
    max_value: float | None = None
    is_editable: bool = True
    updated_at: datetime | None = None


class SettingsResponse(BaseModel):
    settings: list[SettingRead]
    effective: dict[str, Any]
    storage: dict[str, int] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    updates: dict[str, Any] = Field(
        min_length=1, description="Map of setting key to new value."
    )


# ---------------------------------------------------------- import/export
class ImportRowPreview(BaseModel):
    row_number: int
    name: str | None = None
    url: str | None = None
    environment: str | None = None
    tags: list[str] = Field(default_factory=list)
    interval_seconds: int | None = None
    timeout_seconds: int | None = None
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None


class ImportPreviewResponse(BaseModel):
    """Result of validating an uploaded file. Nothing has been written yet."""

    token: str = Field(
        description=(
            "Opaque handle for this preview. Pass it to /api/import/confirm to "
            "create the validated rows."
        )
    )
    expires_at: datetime
    filename: str
    total_rows: int
    valid_count: int
    invalid_count: int
    duplicate_count: int
    detected_columns: list[str] = Field(default_factory=list)
    unknown_columns: list[str] = Field(default_factory=list)
    file_errors: list[str] = Field(default_factory=list)
    rows: list[ImportRowPreview] = Field(default_factory=list)


class ImportConfirmRequest(BaseModel):
    token: str
    row_numbers: list[int] | None = Field(
        default=None,
        description="Subset of rows to import. Omit to import every valid row.",
    )


class ImportResultResponse(BaseModel):
    created_count: int
    failed_count: int
    skipped_count: int
    created: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


# ----------------------------------------------------------------- health
class ComponentHealth(BaseModel):
    status: str
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    database: str
    monitoring_worker: str
    components: dict[str, ComponentHealth] = Field(default_factory=dict)
    checked_at: datetime


class WorkerStatus(BaseModel):
    worker_id: str
    hostname: str | None = None
    version: str | None = None
    started_at: datetime
    last_seen_at: datetime
    seconds_since_heartbeat: float
    is_healthy: bool
    checks_completed: int = 0
    checks_failed: int = 0
    in_flight: int = 0
