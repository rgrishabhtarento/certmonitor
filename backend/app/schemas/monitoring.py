"""Monitoring result, SSL certificate, incident and alert schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# --------------------------------------------------------------- results
class MonitoringResultRead(ORMModel):
    id: int
    endpoint_id: uuid.UUID
    checked_at: datetime
    status: str
    http_status_code: int | None = None

    response_time_ms: float | None = None
    dns_time_ms: float | None = None
    connect_time_ms: float | None = None
    tls_time_ms: float | None = None
    ttfb_ms: float | None = None
    total_time_ms: float | None = None

    resolved_ip: str | None = None
    content_length: int | None = None
    redirect_count: int = 0
    final_url: str | None = None
    redirect_chain: list[dict[str, Any]] | None = None
    response_headers: dict[str, str] | None = None

    error_message: str | None = None
    failure_reason: str
    failure_reason_label: str | None = None

    tls_version: str | None = None
    tls_cipher: str | None = None
    cert_expires_at: datetime | None = None
    ssl_days_remaining: int | None = None
    ssl_status: str | None = None

    is_manual: bool = False
    checked_by: str | None = None


class CheckNowResponse(BaseModel):
    """Response to a manual check.

    ``persisted`` is false for a dry-run test, which lets an operator validate
    a configuration without polluting the history or opening an incident.
    """

    endpoint_id: uuid.UUID
    persisted: bool
    status: str
    previous_status: str | None = None
    http_status_code: int | None = None
    response_time_ms: float | None = None
    dns_time_ms: float | None = None
    connect_time_ms: float | None = None
    tls_time_ms: float | None = None
    resolved_ip: str | None = None
    error_message: str | None = None
    failure_reason: str
    failure_reason_label: str | None = None
    redirect_count: int = 0
    final_url: str | None = None
    content_length: int | None = None
    # Populated when the configured path was absent and discovery found a
    # working one; `path_probes` lists what was tried so the adoption is
    # auditable rather than magic.
    resolved_path: str | None = None
    path_probes: list[dict[str, Any]] = Field(default_factory=list)
    checked_at: datetime
    certificate: "SslCertificateRead | None" = None
    incident_opened: int | None = None
    incident_closed: int | None = None


# ---------------------------------------------------------- certificates
class CertificateChainLink(BaseModel):
    position: int
    subject: str | None = None
    common_name: str | None = None
    issuer: str | None = None
    issuer_common_name: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    is_self_signed: bool = False
    fingerprint_sha256: str | None = None


class SslCertificateRead(ORMModel):
    id: int | None = None
    endpoint_id: uuid.UUID | None = None

    fingerprint_sha256: str | None = None
    serial_number: str | None = None
    subject: str | None = None
    common_name: str | None = None
    issuer: str | None = None
    issuer_common_name: str | None = None
    issuer_organization: str | None = None
    san: list[str] | None = None

    valid_from: datetime | None = None
    valid_to: datetime | None = None
    days_remaining: int | None = None

    signature_algorithm: str | None = None
    key_algorithm: str | None = None
    key_size: int | None = None
    version: str | None = None
    tls_version: str | None = None
    tls_cipher: str | None = None

    is_self_signed: bool = False
    is_wildcard: bool = False
    hostname_matches: bool | None = None
    chain_verified: bool | None = None
    verification_status: str | None = None
    verification_error: str | None = None
    chain: list[dict[str, Any]] | None = None
    chain_length: int | None = None

    status: str
    is_current: bool = True
    first_seen_at: datetime | None = None
    checked_at: datetime | None = None


class SslDashboardRow(BaseModel):
    """One row of the SSL certificates table."""

    endpoint_id: uuid.UUID
    endpoint_name: str
    url: str
    hostname: str
    environment: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None

    common_name: str | None = None
    issuer: str | None = None
    issuer_organization: str | None = None
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    status: str
    is_self_signed: bool = False
    is_wildcard: bool = False
    tls_version: str | None = None
    key_algorithm: str | None = None
    key_size: int | None = None
    signature_algorithm: str | None = None
    hostname_matches: bool | None = None
    chain_verified: bool | None = None
    verification_status: str | None = None
    san_count: int = 0
    checked_at: datetime | None = None


class SslSummary(BaseModel):
    total: int = 0
    valid: int = 0
    expiring_soon: int = 0
    critical: int = 0
    expired: int = 0
    invalid: int = 0
    unable_to_check: int = 0
    self_signed: int = 0
    warning_days: int = 30
    critical_days: int = 7


# -------------------------------------------------------------- incidents
class IncidentEndpointRef(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    environment: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    current_status: str | None = None


class IncidentRead(ORMModel):
    id: int
    endpoint_id: uuid.UUID
    endpoint: IncidentEndpointRef | None = None
    status: str
    severity: str
    started_at: datetime
    resolved_at: datetime | None = None
    duration_seconds: int | None = None
    reason: str | None = None
    reason_label: str | None = None
    error_message: str | None = None
    first_failure_status_code: int | None = None
    failed_check_count: int = 0
    recovery_status_code: int | None = None
    recovery_response_time_ms: float | None = None
    timeline: list[dict[str, Any]] | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    notes: str | None = None
    created_at: datetime


class IncidentUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=4000)
    acknowledge: bool | None = None


def incident_endpoint_ref(endpoint: Any) -> IncidentEndpointRef | None:
    """Flatten an ``Endpoint`` row into the reference an incident carries."""
    if endpoint is None:
        return None
    return IncidentEndpointRef(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.url,
        environment=endpoint.environment.name if endpoint.environment else None,
        tags=endpoint.tag_names,
        owner=endpoint.owner,
        current_status=endpoint.current_status,
    )


def incident_to_schema(
    incident: Any,
    *,
    reason_label: str | None = None,
    acknowledged_by: str | None = None,
) -> IncidentRead:
    """Build an :class:`IncidentRead` from an ``Incident`` row.

    Constructed field by field rather than with ``model_validate``: the row's
    ``endpoint`` attribute is a full ORM object whose ``environment`` and
    ``tags`` are themselves objects, so letting Pydantic coerce it into the
    flattened :class:`IncidentEndpointRef` would fail. Flattening it here keeps
    the shape explicit.
    """
    return IncidentRead(
        id=incident.id,
        endpoint_id=incident.endpoint_id,
        endpoint=incident_endpoint_ref(getattr(incident, "endpoint", None)),
        status=incident.status,
        severity=incident.severity,
        started_at=incident.started_at,
        resolved_at=incident.resolved_at,
        duration_seconds=incident.duration_seconds,
        reason=incident.reason,
        reason_label=reason_label,
        error_message=incident.error_message,
        first_failure_status_code=incident.first_failure_status_code,
        failed_check_count=incident.failed_check_count,
        recovery_status_code=incident.recovery_status_code,
        recovery_response_time_ms=incident.recovery_response_time_ms,
        timeline=incident.timeline,
        acknowledged_at=incident.acknowledged_at,
        acknowledged_by=acknowledged_by,
        notes=incident.notes,
        created_at=incident.created_at,
    )


# ----------------------------------------------------------------- alerts
class AlertRead(ORMModel):
    id: int
    endpoint_id: uuid.UUID | None = None
    endpoint_name: str | None = None
    endpoint_url: str | None = None
    incident_id: int | None = None
    alert_type: str
    severity: str
    title: str
    message: str | None = None
    details: dict[str, Any] | None = None
    is_acknowledged: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    notification_status: str
    notification_attempts: int = 0
    notification_error: str | None = None
    notified_at: datetime | None = None
    created_at: datetime


class AlertAcknowledge(BaseModel):
    alert_ids: list[int] | None = Field(
        default=None,
        description="Alert ids to acknowledge. Omit to acknowledge everything matching the current filters.",
    )


# --------------------------------------------------- notification channels
class NotificationChannelWrite(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    channel_type: str
    is_enabled: bool = True
    config: dict[str, Any] = Field(
        description=(
            "Provider configuration. Stored encrypted; only non-sensitive "
            "fields are returned when reading the channel back."
        )
    )
    min_severity: str = "warning"
    event_types: list[str] | None = None
    environment_filter: list[str] | None = None
    tag_filter: list[str] | None = None


class NotificationChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=96)
    is_enabled: bool | None = None
    config: dict[str, Any] | None = None
    min_severity: str | None = None
    event_types: list[str] | None = None
    environment_filter: list[str] | None = None
    tag_filter: list[str] | None = None


class NotificationChannelRead(ORMModel):
    id: uuid.UUID
    name: str
    channel_type: str
    is_enabled: bool
    config_public: dict[str, Any] | None = None
    min_severity: str
    event_types: list[str] | None = None
    environment_filter: list[str] | None = None
    tag_filter: list[str] | None = None
    last_used_at: datetime | None = None
    last_error: str | None = None
    success_count: int = 0
    failure_count: int = 0
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------- audit logs
class AuditLogRead(ORMModel):
    id: int
    user_id: uuid.UUID | None = None
    username: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    details: dict[str, Any] | None = None
    status: str
    ip_address: str | None = None
    user_agent: str | None = None
    request_method: str | None = None
    request_path: str | None = None
    created_at: datetime


CheckNowResponse.model_rebuild()


# --------------------------------------------------------------- diagnostics
class DiagnosticLayer(BaseModel):
    """One stage of the DNS -> TCP -> TLS -> HTTP chain."""

    layer: str
    status: str = Field(description="ok | warning | failed | skipped")
    detail: str
    elapsed_ms: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DiagnosticFinding(BaseModel):
    severity: str = Field(description="high | medium | low")
    title: str
    detail: str
    action: str = Field(description="The concrete next step to take.")


class DiagnosticEvidence(BaseModel):
    """One fact the conclusion rests on.

    ``kind`` is the honesty mechanism: ``observed`` was measured or read from
    the database, ``inferred`` is a conclusion drawn from observations, and
    ``unknown`` names something relevant that InfraSight cannot see from
    where it runs - so the reader knows to check it themselves rather than
    assuming it was covered.
    """

    label: str
    value: str
    kind: str = Field(description="observed | inferred | unknown")
    status: str | None = Field(default=None, description="ok | warning | failed")
    detail: str | None = None


class DiagnosticCandidate(BaseModel):
    """A possible root cause, with the evidence that scored it.

    ``share`` is the candidate's proportion of total accumulated evidence
    weight. It is not a probability, and is labelled as a weight in the UI -
    putting a decimal point on a handful of heuristics would imply a rigour
    that is not there. ``band`` is the honest version.
    """

    cause: str
    label: str
    explanation: str
    score: int
    share: float
    band: str = Field(description="most_likely | possible | less_likely")
    why: list[str] = Field(default_factory=list)


class DiagnosticAction(BaseModel):
    """Something to do about it, with its blast radius stated.

    Ordered safest first, so an engineer who stops after step two has still
    done the sensible thing. Nothing marked ``high_risk`` is ever executed by
    InfraSight - it is only described.
    """

    step: int
    title: str
    detail: str
    risk: str = Field(description="safe | disruptive | high_risk")
    command: str | None = None
    command_note: str | None = None


class DiagnosticCommand(BaseModel):
    command: str
    note: str
    risk: str = "safe"


class DiagnosticsResponse(BaseModel):
    """Triage for one endpoint.

    Reads top to bottom the way an engineer works: what is wrong, how
    confident we are, the evidence, what changed recently, what to do, and how
    to tell whether it worked.
    """

    endpoint_id: uuid.UUID
    endpoint_name: str
    url: str
    application: str | None = None
    environment: str | None = None
    current_status: str
    generated_at: datetime
    elapsed_ms: float
    focus: str = "auto"
    diagnosis_id: int | None = None

    verdict: str
    summary: str
    root_cause: str | None = None
    confidence: str = Field(description="high | medium | low")
    severity: str = Field(description="info | low | medium | high | critical")
    deepest_layer_ok: str | None = None
    failure_started_at: datetime | None = None

    candidates: list[DiagnosticCandidate] = Field(default_factory=list)
    evidence: list[DiagnosticEvidence] = Field(default_factory=list)
    not_observable: list[DiagnosticEvidence] = Field(
        default_factory=list,
        description="What InfraSight cannot see from outside the endpoint, "
        "stated explicitly so no reader mistakes silence for a clean bill.",
    )
    actions: list[DiagnosticAction] = Field(default_factory=list)
    commands: list[DiagnosticCommand] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)

    layers: list[DiagnosticLayer] = Field(default_factory=list)
    findings: list[DiagnosticFinding] = Field(default_factory=list)
    comparisons: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra probes run for contrast, e.g. the site root, or the "
        "same request with certificate verification disabled.",
    )
    history: dict[str, Any] = Field(default_factory=dict)
    correlation: dict[str, Any] = Field(default_factory=dict)
    changes: dict[str, Any] = Field(
        default_factory=dict,
        description="Deployments near the failure. Correlation only - the "
        "wording throughout never claims causation.",
    )
    incidents: dict[str, Any] = Field(default_factory=dict)
    recurrence: dict[str, Any] = Field(default_factory=dict)
    application_summary: dict[str, Any] | None = None


class DiagnosisHistoryItem(BaseModel):
    """A past diagnosis, for spotting a recurring problem."""

    id: int
    endpoint_id: uuid.UUID
    endpoint_name: str | None = None
    application: str | None = None
    created_at: datetime
    requested_by: str | None = None
    focus: str

    verdict: str
    severity: str
    confidence: str
    headline: str
    root_cause: str | None = None
    endpoint_status: str | None = None
    deepest_layer_ok: str | None = None
    http_status_code: int | None = None
    response_time_ms: float | None = None
    incident_id: int | None = None
    change_id: int | None = None

    resolution: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class DiagnosisResolution(BaseModel):
    """What actually fixed it.

    The one field the engine cannot derive. Recorded by the operator and
    surfaced verbatim the next time the same verdict comes back.
    """

    resolution: str = Field(min_length=1, max_length=4000)
