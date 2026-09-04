"""Runtime-configurable settings.

Environment variables supply the boot defaults. Rows in ``system_settings``
override them so an operator can retune thresholds from the UI without a
redeploy. Values are cached in-process for a few seconds - the worker reads
them on every check cycle, and a database round trip per endpoint would be
wasteful.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as env_settings
from app.core.logging import get_logger
# The checker owns the default path list because it owns the probing; it
# imports nothing from `app.services`, so there is no cycle here.
from app.monitoring.checker import DEFAULT_HEALTH_PATHS
from app.models.system import SystemSetting

logger = get_logger(__name__)

ValueType = Literal["int", "float", "bool", "string", "json"]

CACHE_TTL_SECONDS = 10.0


@dataclass(frozen=True)
class SettingSpec:
    key: str
    value_type: ValueType
    default: Any
    category: str
    label: str
    description: str
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: list[Any] | None = None
    is_editable: bool = True


# Intervals offered in the UI. Anything below MIN_MONITOR_INTERVAL is filtered
# out at read time so an aggressive value cannot be selected even if the row is
# edited directly.
_INTERVAL_CHOICES = [30, 60, 300, 600, 1800, 3600]
_RETENTION_CHOICES = [7, 30, 90, 180, 365]

SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="default_monitor_interval",
        value_type="int",
        default=env_settings.DEFAULT_MONITOR_INTERVAL,
        category="monitoring",
        label="Default monitoring interval (seconds)",
        description="Applied to new endpoints that do not specify their own interval.",
        min_value=env_settings.MIN_MONITOR_INTERVAL,
        max_value=env_settings.MAX_MONITOR_INTERVAL,
        allowed_values=_INTERVAL_CHOICES,
    ),
    SettingSpec(
        key="default_timeout",
        value_type="int",
        default=env_settings.DEFAULT_TIMEOUT,
        category="monitoring",
        label="Default timeout (seconds)",
        description="How long a single check waits before it is treated as failed.",
        min_value=1,
        max_value=120,
    ),
    SettingSpec(
        key="failure_threshold",
        value_type="int",
        default=env_settings.FAILURE_THRESHOLD,
        category="monitoring",
        label="Consecutive failures before an incident opens",
        description=(
            "A single blip does not open an incident. This many consecutive "
            "failed checks are required."
        ),
        min_value=1,
        max_value=20,
    ),
    SettingSpec(
        key="response_time_threshold_ms",
        value_type="int",
        default=env_settings.RESPONSE_TIME_THRESHOLD_MS,
        category="monitoring",
        label="Response time threshold (ms)",
        description="Slower successful responses are reported as degraded.",
        min_value=50,
        max_value=120000,
    ),
    SettingSpec(
        key="recovery_threshold",
        value_type="int",
        default=1,
        category="monitoring",
        label="Consecutive successes before recovery",
        description="Guards against flapping endpoints closing incidents too early.",
        min_value=1,
        max_value=10,
    ),
    SettingSpec(
        key="ssl_warning_days",
        value_type="int",
        default=env_settings.SSL_WARNING_DAYS,
        category="ssl",
        label="SSL warning threshold (days)",
        description="Certificates with fewer remaining days are marked Expiring Soon.",
        min_value=1,
        max_value=365,
    ),
    SettingSpec(
        key="ssl_critical_days",
        value_type="int",
        default=env_settings.SSL_CRITICAL_DAYS,
        category="ssl",
        label="SSL critical threshold (days)",
        description="Certificates with fewer remaining days are marked Critical.",
        min_value=1,
        max_value=180,
    ),
    SettingSpec(
        key="alert_cooldown_minutes",
        value_type="int",
        default=env_settings.ALERT_COOLDOWN_MINUTES,
        category="alerting",
        label="Alert cooldown (minutes)",
        description=(
            "Suppresses repeat alerts of the same type for the same endpoint "
            "within this window."
        ),
        min_value=0,
        max_value=1440,
    ),
    SettingSpec(
        key="health_path_discovery",
        value_type="bool",
        default=True,
        category="monitoring",
        label="Find the health path automatically",
        description=(
            "When the configured URL returns 404, try the common health paths "
            "below and adopt the first that answers. Only a missing path "
            "triggers this - a 5xx, a timeout or a TLS error is still reported "
            "as the failure it is."
        ),
    ),
    SettingSpec(
        key="health_path_candidates",
        value_type="json",
        default=list(DEFAULT_HEALTH_PATHS),
        category="monitoring",
        label="Health paths to try",
        description=(
            "Tried in order, comma-separated. The first that returns an "
            "expected status is remembered per endpoint, so the search runs "
            "once rather than every interval."
        ),
    ),
    SettingSpec(
        key="latency_anomaly_multiplier",
        value_type="float",
        default=3.0,
        category="monitoring",
        label="Latency anomaly multiplier",
        description=(
            "How many times its own baseline an endpoint has to slow down "
            "before it is reported as degraded. Below this it is treated as "
            "normal variation rather than a finding."
        ),
        min_value=1.5,
        max_value=50,
    ),
    SettingSpec(
        key="recovery_checks_required",
        value_type="int",
        default=3,
        category="monitoring",
        label="Healthy checks required for recovery",
        description=(
            "How many consecutive passing checks a diagnosis asks for before "
            "calling something resolved. One passing probe is not a recovery."
        ),
        min_value=1,
        max_value=20,
    ),
    SettingSpec(
        key="deployment_correlation_minutes",
        value_type="int",
        default=30,
        category="monitoring",
        label="Deployment correlation window (minutes)",
        description=(
            "A deployment finishing within this long before a failure begins "
            "is reported as correlated. Always described as a correlation, "
            "never as a confirmed cause."
        ),
        min_value=1,
        max_value=1440,
    ),
    SettingSpec(
        key="incident_grouping_minutes",
        value_type="int",
        default=15,
        category="monitoring",
        label="Incident grouping window (minutes)",
        description=(
            "Failures on one endpoint inside this window are treated as one "
            "problem rather than several, which is what keeps a single outage "
            "from producing a wall of alerts."
        ),
        min_value=1,
        max_value=1440,
    ),
    SettingSpec(
        key="rca_reminder_days",
        value_type="int",
        default=7,
        category="rca",
        label="RCA reminder period (days)",
        description=(
            "An open RCA older than this is highlighted. RCA is never "
            "mandatory - this only surfaces a backlog, it does not chase it."
        ),
        min_value=1,
        max_value=180,
    ),
    SettingSpec(
        key="rca_default_due_days",
        value_type="int",
        default=0,
        category="rca",
        label="Default RCA due period (days)",
        description=(
            "Applied when an RCA is requested without a due date. Zero means "
            "no default deadline, and an RCA without a deadline is never "
            "overdue."
        ),
        min_value=0,
        max_value=180,
    ),
    SettingSpec(
        key="alerts_enabled",
        value_type="bool",
        default=True,
        category="alerting",
        label="Alerting enabled",
        description="Master switch for alert generation and notification delivery.",
    ),
    SettingSpec(
        key="alert_on_degraded",
        value_type="bool",
        default=True,
        category="alerting",
        label="Alert on degraded response time",
        description="Raise an alert when a healthy endpoint breaches its latency threshold.",
    ),
    SettingSpec(
        key="notifications_enabled",
        value_type="bool",
        default=True,
        category="alerting",
        label="Send notifications",
        description="When off, alerts are still recorded but nothing is dispatched.",
    ),
    SettingSpec(
        key="data_retention_days",
        value_type="int",
        default=env_settings.DATA_RETENTION_DAYS,
        category="retention",
        label="Monitoring data retention (days)",
        description=(
            "High-frequency check results older than this are deleted. "
            "Incidents and audit logs are kept."
        ),
        min_value=1,
        max_value=3650,
        allowed_values=_RETENTION_CHOICES,
    ),
    SettingSpec(
        key="incident_retention_days",
        value_type="int",
        default=730,
        category="retention",
        label="Incident retention (days)",
        description="Resolved incidents older than this are deleted.",
        min_value=30,
        max_value=3650,
    ),
    SettingSpec(
        key="audit_retention_days",
        value_type="int",
        default=365,
        category="retention",
        label="Audit log retention (days)",
        description="Audit entries older than this are deleted.",
        min_value=30,
        max_value=3650,
    ),
    SettingSpec(
        key="alert_retention_days",
        value_type="int",
        default=180,
        category="retention",
        label="Alert retention (days)",
        description="Acknowledged alerts older than this are deleted.",
        min_value=7,
        max_value=3650,
    ),
    SettingSpec(
        key="uptime_sla_target",
        value_type="float",
        default=99.9,
        category="general",
        label="Uptime SLA target (%)",
        description="Shown on the dashboard as the line to compare uptime against.",
        min_value=0,
        max_value=100,
    ),
    SettingSpec(
        key="change_approval_environments",
        value_type="json",
        default=["production"],
        category="changes",
        label="Environments that require approval",
        description=(
            "A change targeting one of these must be approved before it can be "
            "deployed. Others can be deployed straight from draft."
        ),
    ),
    SettingSpec(
        key="change_health_check_on_resume",
        value_type="bool",
        default=True,
        category="changes",
        label="Health-check after a deployment ends",
        description=(
            "Run an immediate check on the affected endpoints as soon as "
            "monitoring resumes, instead of waiting for the next scheduled one."
        ),
    ),
    SettingSpec(
        key="change_max_pause_minutes",
        value_type="int",
        default=240,
        category="changes",
        label="Maximum deployment pause (minutes)",
        description=(
            "A safety net: monitoring paused by a deployment for longer than "
            "this is flagged, so a forgotten deployment cannot silence an "
            "endpoint indefinitely."
        ),
        min_value=5,
        max_value=1440,
    ),
    SettingSpec(
        key="allowed_intervals",
        value_type="json",
        default=_INTERVAL_CHOICES,
        category="monitoring",
        label="Selectable monitoring intervals",
        description="Interval options offered when configuring an endpoint.",
    ),
)

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTING_SPECS}

_cache: dict[str, Any] | None = None
_cache_expires_at: float = 0.0


# ---------------------------------------------------------------- coercion
def coerce(value: Any, value_type: str) -> Any:
    if value is None:
        return None
    if value_type == "int":
        return int(float(value))
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "json":
        if isinstance(value, (list, dict)):
            return value
        return json.loads(value)
    return str(value)


def serialise(value: Any, value_type: str) -> str:
    if value_type == "json":
        return json.dumps(value)
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


def validate_value(spec: SettingSpec, value: Any) -> Any:
    """Coerce and range-check a value against its spec."""
    try:
        coerced = coerce(value, spec.value_type)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{spec.key} must be a valid {spec.value_type}") from exc

    if coerced is None:
        raise ValueError(f"{spec.key} must not be empty")

    if spec.value_type in ("int", "float"):
        if spec.min_value is not None and coerced < spec.min_value:
            raise ValueError(f"{spec.key} must be at least {spec.min_value:g}")
        if spec.max_value is not None and coerced > spec.max_value:
            raise ValueError(f"{spec.key} must be at most {spec.max_value:g}")

    if spec.key == "allowed_intervals":
        if not isinstance(coerced, list) or not coerced:
            raise ValueError("allowed_intervals must be a non-empty list of seconds")
        cleaned = sorted({int(v) for v in coerced})
        if any(v < env_settings.MIN_MONITOR_INTERVAL for v in cleaned):
            raise ValueError(
                "intervals below "
                f"{env_settings.MIN_MONITOR_INTERVAL}s are not permitted"
            )
        return cleaned

    return coerced


def defaults() -> dict[str, Any]:
    return {spec.key: spec.default for spec in SETTING_SPECS}


# ------------------------------------------------------------------- reads
async def load_settings(session: AsyncSession, *, use_cache: bool = True) -> dict[str, Any]:
    """Return the effective settings map (defaults overlaid with DB rows)."""
    global _cache, _cache_expires_at

    now = time.monotonic()
    if use_cache and _cache is not None and now < _cache_expires_at:
        return dict(_cache)

    effective = defaults()
    try:
        rows = (await session.execute(select(SystemSetting))).scalars().all()
    except Exception as exc:  # pragma: no cover - keeps checks running
        logger.warning("settings_load_failed", error=str(exc))
        return effective

    for row in rows:
        spec = SPEC_BY_KEY.get(row.key)
        if spec is None:
            continue
        try:
            effective[row.key] = coerce(row.value, spec.value_type)
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("settings_row_invalid", key=row.key)

    # Cross-field sanity: a critical threshold above the warning threshold
    # would leave the "Warning" band empty.
    if effective["ssl_critical_days"] > effective["ssl_warning_days"]:
        effective["ssl_critical_days"] = effective["ssl_warning_days"]

    intervals = [
        int(v)
        for v in effective.get("allowed_intervals", _INTERVAL_CHOICES)
        if int(v) >= env_settings.MIN_MONITOR_INTERVAL
    ]
    effective["allowed_intervals"] = sorted(set(intervals)) or [
        env_settings.MIN_MONITOR_INTERVAL
    ]

    _cache = dict(effective)
    _cache_expires_at = now + CACHE_TTL_SECONDS
    return effective


def invalidate_cache() -> None:
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


# ------------------------------------------------------------------ writes
async def ensure_seeded(session: AsyncSession) -> int:
    """Insert any spec that has no row yet. Idempotent."""
    existing = set(
        (await session.execute(select(SystemSetting.key))).scalars().all()
    )
    created = 0
    for spec in SETTING_SPECS:
        if spec.key in existing:
            continue
        session.add(
            SystemSetting(
                key=spec.key,
                value=serialise(spec.default, spec.value_type),
                value_type=spec.value_type,
                category=spec.category,
                label=spec.label,
                description=spec.description,
                allowed_values=spec.allowed_values,
                min_value=spec.min_value,
                max_value=spec.max_value,
                is_editable=spec.is_editable,
            )
        )
        created += 1
    if created:
        await session.flush()
    invalidate_cache()
    return created


async def update_settings(
    session: AsyncSession,
    updates: dict[str, Any],
    *,
    user_id: Any | None = None,
) -> dict[str, Any]:
    """Validate and persist a batch of setting changes.

    Raises ``ValueError`` describing the first problem found; nothing is
    written unless every value validates.
    """
    unknown = [key for key in updates if key not in SPEC_BY_KEY]
    if unknown:
        raise ValueError("unknown setting(s): " + ", ".join(sorted(unknown)))

    validated: dict[str, Any] = {}
    for key, raw in updates.items():
        spec = SPEC_BY_KEY[key]
        if not spec.is_editable:
            raise ValueError(f"{key} is not editable at runtime")
        validated[key] = validate_value(spec, raw)

    merged = {**defaults(), **await load_settings(session, use_cache=False), **validated}
    if merged["ssl_critical_days"] > merged["ssl_warning_days"]:
        raise ValueError(
            "SSL critical threshold must be less than or equal to the warning threshold"
        )

    rows = (
        await session.execute(
            select(SystemSetting).where(SystemSetting.key.in_(list(validated)))
        )
    ).scalars().all()
    by_key = {row.key: row for row in rows}

    for key, value in validated.items():
        spec = SPEC_BY_KEY[key]
        row = by_key.get(key)
        payload = serialise(value, spec.value_type)
        if row is None:
            session.add(
                SystemSetting(
                    key=key,
                    value=payload,
                    value_type=spec.value_type,
                    category=spec.category,
                    label=spec.label,
                    description=spec.description,
                    allowed_values=spec.allowed_values,
                    min_value=spec.min_value,
                    max_value=spec.max_value,
                    updated_by_id=user_id,
                )
            )
        else:
            row.value = payload
            row.updated_by_id = user_id

    await session.flush()
    invalidate_cache()
    return validated
