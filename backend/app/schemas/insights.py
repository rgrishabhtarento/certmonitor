"""Schemas for the locally computed operational intelligence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AttentionItem(BaseModel):
    """One thing worth looking at, already prioritised."""

    priority: str = Field(description="critical | high | medium | low")
    kind: str
    title: str
    detail: str
    endpoint_id: str | None = None
    application: str | None = None
    environment: str | None = None
    change_reference: str | None = None


class SmartSummary(BaseModel):
    """The "what needs my attention" view.

    Every number is a count of real rows. The health score is a weighted
    blend of four measured components, and both the components and the plain
    reasons behind the figure are returned with it - a score with no reasons
    is a number nobody can act on.
    """

    generated_at: datetime
    health_score: int
    health_components: dict[str, float] = Field(default_factory=dict)
    health_reasons: list[str] = Field(default_factory=list)

    monitored: int = 0
    up: int = 0
    down: int = 0
    degraded: int = 0
    critical_production_down: int = 0
    ssl_attention: int = 0
    open_incidents: int = 0
    recent_deployments: int = 0
    active_deployments: int = 0
    rca_pending: int = 0

    deployment_incident_correlations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Incidents that began shortly after a deployment finished. "
        "Timing only - never presented as confirmed causation.",
    )
    performance_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    attention: list[AttentionItem] = Field(default_factory=list)


class DailySummary(BaseModel):
    generated_at: datetime
    window_hours: int
    endpoints_monitored: int = 0
    endpoints_healthy_throughout: int = 0
    incidents: int = 0
    incidents_resolved: int = 0
    deployments: int = 0
    deployments_failed: int = 0
    ssl_issues: int = 0
    rca_pending: int = 0
    deployment_incident_correlations: int = 0
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    """A parsed infrastructure question and its rows.

    ``understood`` and ``intent`` are returned so the UI can show what the
    parser made of the question. A misread question is then obvious, rather
    than silently returning the wrong rows with confidence.
    """

    understood: bool
    intent: str | None = None
    description: str
    count: int = 0
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
