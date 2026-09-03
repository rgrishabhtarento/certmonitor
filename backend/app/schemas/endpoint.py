"""Endpoint, tag and environment schemas.

The read models deliberately expose ``has_auth_secret`` and
``auth_secret_hint`` instead of the credential itself: once saved, an
endpoint's token or password is never returned by the API in any form.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.enums import AuthType, CheckType, EndpointStatus, SslStatus
from app.monitoring.validators import (
    UrlValidationError,
    normalise_status_codes,
    parse_target,
)
from app.schemas.common import ORMModel

HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


# Shared field normalisers. Defined at module level so the create and update
# schemas apply identical rules without one inheriting the other's required
# fields.
def _normalise_url(value: str) -> str:
    try:
        return parse_target(value).url
    except UrlValidationError as exc:
        raise ValueError(str(exc)) from exc


def _normalise_method(value: str) -> str:
    value = value.strip().upper()
    if value not in HTTP_METHODS:
        raise ValueError(
            "http_method must be one of: " + ", ".join(sorted(HTTP_METHODS))
        )
    return value


def _normalise_check_type(value: str) -> str:
    value = value.strip().lower()
    if value not in {t.value for t in CheckType}:
        raise ValueError(
            "check_type must be one of: " + ", ".join(t.value for t in CheckType)
        )
    return value


def _normalise_auth_type(value: str) -> str:
    value = value.strip().lower()
    if value not in {t.value for t in AuthType}:
        raise ValueError(
            "auth_type must be one of: " + ", ".join(t.value for t in AuthType)
        )
    return value


def _normalise_expected_status(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return normalise_status_codes(value)
    except UrlValidationError as exc:
        raise ValueError(str(exc)) from exc


def _dedupe_tags(value: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in value:
        name = str(tag).strip().lower()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


# ------------------------------------------------------------------- tags
class TagRead(ORMModel):
    id: uuid.UUID
    name: str
    color: str | None = None
    description: str | None = None
    endpoint_count: int = 0
    created_at: datetime | None = None


class TagWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def _normalise(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("tag name must not be empty")
        if any(ch in value for ch in (",", ";", "|")):
            raise ValueError("tag name must not contain commas, semicolons or pipes")
        return value


# ----------------------------------------------------------- environments
class EnvironmentRead(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str | None = None
    description: str | None = None
    color: str | None = None
    sort_order: int = 100
    is_active: bool = True
    endpoint_count: int = 0
    created_at: datetime | None = None


class EnvironmentWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=16)
    sort_order: int = Field(default=100, ge=0, le=10_000)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


# -------------------------------------------------------------- endpoints
class EndpointBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2048)
    check_type: str = Field(default=CheckType.HTTP.value)
    http_method: str = Field(default="GET")
    port: int | None = Field(default=None, ge=1, le=65535)

    environment: str | None = Field(
        default=None, description="Environment name or id."
    )
    tags: list[str] = Field(default_factory=list, max_length=20)
    description: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    team: str | None = Field(default=None, max_length=128)
    application: str | None = Field(default=None, max_length=128)

    monitoring_enabled: bool = True
    is_paused: bool = False
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)

    expected_status_codes: str | None = Field(default=None, max_length=128)
    expected_body_substring: str | None = Field(default=None, max_length=255)
    follow_redirects: bool = True
    verify_ssl: bool = True
    ssl_monitoring_enabled: bool = True
    request_body: str | None = Field(default=None, max_length=64_000)
    custom_headers: dict[str, str] | None = None

    auth_type: str = Field(default=AuthType.NONE.value)
    auth_username: str | None = Field(default=None, max_length=128)
    auth_header_name: str | None = Field(default=None, max_length=128)
    auth_secret: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Bearer token, basic-auth password or custom header value. "
            "Stored encrypted and never returned by the API."
        ),
    )

    failure_threshold: int | None = Field(default=None, ge=1, le=20)
    response_time_threshold_ms: int | None = Field(default=None, ge=1, le=600_000)
    ssl_warning_days: int | None = Field(default=None, ge=1, le=365)
    ssl_critical_days: int | None = Field(default=None, ge=1, le=180)
    alerts_enabled: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _normalise_url(value)

    @field_validator("http_method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        return _normalise_method(value)

    @field_validator("check_type")
    @classmethod
    def _validate_check_type(cls, value: str) -> str:
        return _normalise_check_type(value)

    @field_validator("auth_type")
    @classmethod
    def _validate_auth_type(cls, value: str) -> str:
        return _normalise_auth_type(value)

    @field_validator("expected_status_codes")
    @classmethod
    def _validate_status_codes(cls, value: str | None) -> str | None:
        return _normalise_expected_status(value)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        return _dedupe_tags(value)

    @model_validator(mode="after")
    def _check_ssl_thresholds(self) -> "EndpointBase":
        if (
            self.ssl_warning_days is not None
            and self.ssl_critical_days is not None
            and self.ssl_critical_days > self.ssl_warning_days
        ):
            raise ValueError(
                "ssl_critical_days must be less than or equal to ssl_warning_days"
            )
        if self.timeout_seconds and self.interval_seconds:
            if self.timeout_seconds > self.interval_seconds:
                raise ValueError(
                    "timeout_seconds must not exceed interval_seconds, or checks "
                    "would overlap"
                )
        return self


class EndpointCreate(EndpointBase):
    pass


class EndpointUpdate(BaseModel):
    """Partial update. Omitted fields are left untouched.

    Omitting ``auth_secret`` keeps the stored credential; sending
    ``auth_type: "none"`` clears it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    check_type: str | None = None
    http_method: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    environment: str | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    description: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    team: str | None = Field(default=None, max_length=128)
    application: str | None = Field(default=None, max_length=128)
    monitoring_enabled: bool | None = None
    is_paused: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    expected_status_codes: str | None = Field(default=None, max_length=128)
    expected_body_substring: str | None = Field(default=None, max_length=255)
    follow_redirects: bool | None = None
    verify_ssl: bool | None = None
    ssl_monitoring_enabled: bool | None = None
    request_body: str | None = Field(default=None, max_length=64_000)
    custom_headers: dict[str, str] | None = None
    auth_type: str | None = None
    auth_username: str | None = Field(default=None, max_length=128)
    auth_header_name: str | None = Field(default=None, max_length=128)
    auth_secret: str | None = Field(default=None, max_length=4096)
    failure_threshold: int | None = Field(default=None, ge=1, le=20)
    response_time_threshold_ms: int | None = Field(default=None, ge=1, le=600_000)
    ssl_warning_days: int | None = Field(default=None, ge=1, le=365)
    ssl_critical_days: int | None = Field(default=None, ge=1, le=180)
    alerts_enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        return None if value is None else _normalise_url(value)

    @field_validator("http_method")
    @classmethod
    def _validate_method(cls, value: str | None) -> str | None:
        return None if value is None else _normalise_method(value)

    @field_validator("check_type")
    @classmethod
    def _validate_check_type(cls, value: str | None) -> str | None:
        return None if value is None else _normalise_check_type(value)

    @field_validator("auth_type")
    @classmethod
    def _validate_auth_type(cls, value: str | None) -> str | None:
        return None if value is None else _normalise_auth_type(value)

    @field_validator("expected_status_codes")
    @classmethod
    def _validate_status_codes(cls, value: str | None) -> str | None:
        return _normalise_expected_status(value)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _dedupe_tags(value)


class EndpointListItem(ORMModel):
    """Compact row for the endpoints table and dashboard lists."""

    id: uuid.UUID
    name: str
    url: str
    protocol: str
    hostname: str
    port: int
    check_type: str
    http_method: str
    environment: EnvironmentRead | None = None
    tags: list[TagRead] = Field(default_factory=list)
    owner: str | None = None
    team: str | None = None
    application: str | None = None

    monitoring_enabled: bool
    is_paused: bool
    # Populated when a deployment paused this endpoint, e.g.
    # "Deployment CHG-2026-0001" - so a paused row explains itself.
    pause_reason: str | None = None
    paused_by_change_id: int | None = None
    # Set when the configured path 404'd and a different one answered, e.g.
    # "/actuator/health". The endpoint's own `url` is left as configured.
    resolved_health_path: str | None = None
    interval_seconds: int
    timeout_seconds: int

    current_status: str
    last_checked_at: datetime | None = None
    last_status_code: int | None = None
    last_response_time_ms: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    ssl_monitoring_enabled: bool = True
    ssl_status: str
    ssl_expires_at: datetime | None = None
    ssl_days_remaining: int | None = None
    ssl_issuer: str | None = None
    ssl_common_name: str | None = None

    uptime_percent_24h: float | None = None
    has_open_incident: bool = False
    created_at: datetime
    updated_at: datetime


class EndpointRead(EndpointListItem):
    """Full endpoint view, including configuration."""

    path: str
    description: str | None = None
    expected_status_codes: str
    expected_body_substring: str | None = None
    follow_redirects: bool
    verify_ssl: bool
    request_body: str | None = None
    custom_headers: dict[str, str] | None = None

    auth_type: str
    auth_username: str | None = None
    auth_header_name: str | None = None
    has_auth_secret: bool = False
    auth_secret_hint: str | None = None

    failure_threshold: int
    response_time_threshold_ms: int | None = None
    ssl_warning_days: int | None = None
    ssl_critical_days: int | None = None
    alerts_enabled: bool

    total_checks: int = 0
    total_failures: int = 0
    next_check_at: datetime | None = None
    created_by: str | None = None
    updated_by: str | None = None


class EndpointStatusUpdate(BaseModel):
    monitoring_enabled: bool | None = None
    is_paused: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "EndpointStatusUpdate":
        if self.monitoring_enabled is None and self.is_paused is None:
            raise ValueError(
                "provide monitoring_enabled and/or is_paused"
            )
        return self


class BulkEndpointAction(BaseModel):
    endpoint_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: str = Field(
        description="One of: enable, disable, pause, resume, delete, check, tag, untag"
    )
    tags: list[str] | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {
            "enable",
            "disable",
            "pause",
            "resume",
            "delete",
            "check",
            "tag",
            "untag",
        }
        if value not in allowed:
            raise ValueError("action must be one of: " + ", ".join(sorted(allowed)))
        return value

    @model_validator(mode="after")
    def _tags_required(self) -> "BulkEndpointAction":
        if self.action in ("tag", "untag") and not self.tags:
            raise ValueError(f"the '{self.action}' action requires tags")
        return self


class EndpointFilterOptions(BaseModel):
    """Everything the filter bar needs, in one request."""

    environments: list[EnvironmentRead] = Field(default_factory=list)
    tags: list[TagRead] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)
    applications: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(
        default_factory=lambda: [s.value for s in EndpointStatus]
    )
    ssl_statuses: list[str] = Field(
        default_factory=lambda: [s.value for s in SslStatus]
    )
    check_types: list[str] = Field(
        default_factory=lambda: [t.value for t in CheckType]
    )
    allowed_intervals: list[int] = Field(default_factory=list)


def endpoint_to_list_item(
    endpoint: Any,
    *,
    uptime_percent: float | None = None,
    has_open_incident: bool = False,
) -> EndpointListItem:
    item = EndpointListItem.model_validate(endpoint)
    item.uptime_percent_24h = uptime_percent
    item.has_open_incident = has_open_incident
    return item


def endpoint_to_read(
    endpoint: Any,
    *,
    uptime_percent: float | None = None,
    has_open_incident: bool = False,
    created_by: str | None = None,
    updated_by: str | None = None,
) -> EndpointRead:
    model = EndpointRead.model_validate(endpoint)
    model.has_auth_secret = bool(endpoint.auth_secret_encrypted)
    model.uptime_percent_24h = uptime_percent
    model.has_open_incident = has_open_incident
    model.created_by = created_by
    model.updated_by = updated_by
    return model
