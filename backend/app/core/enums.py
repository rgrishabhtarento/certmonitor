"""Domain enumerations.

These are stored as short strings in PostgreSQL rather than native ENUM types:
adding a value later is then a code change instead of a migration that has to
mutate a type under load.
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    ADMIN = "admin"
    APPROVER = "approver"
    VIEWER = "viewer"


class Permission(StrEnum):
    ENDPOINT_READ = "endpoint:read"
    ENDPOINT_WRITE = "endpoint:write"
    ENDPOINT_DELETE = "endpoint:delete"
    ENDPOINT_CHECK = "endpoint:check"
    ENDPOINT_IMPORT = "endpoint:import"
    ENDPOINT_EXPORT = "endpoint:export"
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    USER_DELETE = "user:delete"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    ALERT_READ = "alert:read"
    ALERT_WRITE = "alert:write"
    INCIDENT_READ = "incident:read"
    INCIDENT_WRITE = "incident:write"
    AUDIT_READ = "audit:read"
    TAG_WRITE = "tag:write"
    ENVIRONMENT_WRITE = "environment:write"
    NOTIFICATION_WRITE = "notification:write"
    # --- change management ---
    CHANGE_READ = "change:read"
    CHANGE_WRITE = "change:write"
    CHANGE_APPROVE = "change:approve"
    CHANGE_DEPLOY = "change:deploy"
    CHANGE_CANCEL = "change:cancel"
    CHANGE_COMMENT = "change:comment"


# Everyone who can see a change can also raise one and comment on it - the
# change record is the team's shared conversation, so gating comments behind a
# role would just push the discussion into chat.
_CHANGE_BASE = {
    Permission.CHANGE_READ.value,
    Permission.CHANGE_WRITE.value,
    Permission.CHANGE_COMMENT.value,
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    RoleName.ADMIN: {p.value for p in Permission},
    # Approves changes and sees the monitoring context needed to judge them,
    # but cannot deploy or alter monitoring configuration.
    RoleName.APPROVER: _CHANGE_BASE
    | {
        Permission.CHANGE_APPROVE.value,
        Permission.ENDPOINT_READ.value,
        Permission.ENDPOINT_EXPORT.value,
        Permission.ALERT_READ.value,
        Permission.INCIDENT_READ.value,
        Permission.SETTINGS_READ.value,
    },
    RoleName.VIEWER: _CHANGE_BASE
    | {
        Permission.ENDPOINT_READ.value,
        Permission.ENDPOINT_EXPORT.value,
        Permission.ALERT_READ.value,
        Permission.INCIDENT_READ.value,
        Permission.SETTINGS_READ.value,
    },
}


class CheckType(StrEnum):
    """Kind of probe to run. HTTP is the default; TCP/TLS allow monitoring
    non-HTTP services without changing the scheduler or storage model."""

    HTTP = "http"
    TCP = "tcp"
    TLS = "tls"


class EndpointStatus(StrEnum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    PAUSED = "paused"


class CheckStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"


class FailureReason(StrEnum):
    NONE = "none"
    DNS_FAILURE = "dns_failure"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_TIMEOUT = "connection_timeout"
    READ_TIMEOUT = "read_timeout"
    TLS_ERROR = "tls_error"
    CERT_EXPIRED = "cert_expired"
    CERT_INVALID = "cert_invalid"
    HTTP_STATUS_MISMATCH = "http_status_mismatch"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    SLOW_RESPONSE = "slow_response"
    BLOCKED_TARGET = "blocked_target"
    CONFIG_ERROR = "config_error"
    UNKNOWN_ERROR = "unknown_error"


class SslStatus(StrEnum):
    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"
    CRITICAL = "critical"
    EXPIRED = "expired"
    INVALID = "invalid"
    UNABLE_TO_CHECK = "unable_to_check"
    NOT_APPLICABLE = "not_applicable"


class IncidentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class AlertType(StrEnum):
    ENDPOINT_DOWN = "endpoint_down"
    ENDPOINT_RECOVERED = "endpoint_recovered"
    HIGH_RESPONSE_TIME = "high_response_time"
    REPEATED_FAILURES = "repeated_failures"
    SSL_EXPIRING = "ssl_expiring"
    SSL_EXPIRED = "ssl_expired"
    SSL_INVALID = "ssl_invalid"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


SEVERITY_ORDER: dict[str, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


class NotificationChannelType(StrEnum):
    WEBHOOK = "webhook"
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    PAGERDUTY = "pagerduty"


class AuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"


class AuditAction(StrEnum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DELETED = "user_deleted"
    USER_ENABLED = "user_enabled"
    USER_DISABLED = "user_disabled"
    ROLE_CHANGED = "role_changed"
    ENDPOINT_CREATED = "endpoint_created"
    ENDPOINT_UPDATED = "endpoint_updated"
    ENDPOINT_DELETED = "endpoint_deleted"
    ENDPOINT_CHECKED = "endpoint_checked"
    ENDPOINTS_IMPORTED = "endpoints_imported"
    ENDPOINTS_EXPORTED = "endpoints_exported"
    SETTINGS_CHANGED = "settings_changed"
    TAG_CREATED = "tag_created"
    TAG_DELETED = "tag_deleted"
    ENVIRONMENT_CREATED = "environment_created"
    ENVIRONMENT_UPDATED = "environment_updated"
    ENVIRONMENT_DELETED = "environment_deleted"
    NOTIFICATION_CHANNEL_CREATED = "notification_channel_created"
    NOTIFICATION_CHANNEL_UPDATED = "notification_channel_updated"
    NOTIFICATION_CHANNEL_DELETED = "notification_channel_deleted"
    ALERT_ACKNOWLEDGED = "alert_acknowledged"
    INCIDENT_UPDATED = "incident_updated"
    INCIDENT_COMMENTED = "incident_commented"
    RCA_REQUESTED = "rca_requested"
    RCA_ASSIGNED = "rca_assigned"
    RCA_UPDATED = "rca_updated"
    RCA_COMPLETED = "rca_completed"
    RCA_NOT_REQUIRED = "rca_not_required"
    CHANGE_CREATED = "change_created"
    CHANGE_UPDATED = "change_updated"
    CHANGE_SUBMITTED = "change_submitted"
    CHANGE_APPROVED = "change_approved"
    CHANGE_REJECTED = "change_rejected"
    CHANGE_CANCELLED = "change_cancelled"
    CHANGE_COMMENTED = "change_commented"
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    DEPLOYMENT_FAILED = "deployment_failed"
    MONITORING_PAUSED = "monitoring_paused"
    MONITORING_RESUMED = "monitoring_resumed"


class ChangeStatus(StrEnum):
    """Deliberately short. Every extra state is one more thing a small team
    has to remember, and none of these can be skipped."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYMENT_IN_PROGRESS = "deployment_in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# States in which a change still owns its endpoints' monitoring pause.
ACTIVE_DEPLOYMENT_STATUSES = (ChangeStatus.DEPLOYMENT_IN_PROGRESS.value,)

# States from which nothing further can happen.
TERMINAL_CHANGE_STATUSES = (
    ChangeStatus.COMPLETED.value,
    ChangeStatus.FAILED.value,
    ChangeStatus.CANCELLED.value,
    ChangeStatus.REJECTED.value,
)


class ChangeRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChangeAction(StrEnum):
    """Entries in a change's activity timeline."""

    CREATED = "created"
    UPDATED = "updated"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYMENT_STARTED = "deployment_started"
    MONITORING_PAUSED = "monitoring_paused"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    DEPLOYMENT_FAILED = "deployment_failed"
    MONITORING_RESUMED = "monitoring_resumed"
    HEALTH_CHECK = "health_check"
    CANCELLED = "cancelled"
    COMMENTED = "commented"


# ------------------------------------------------------- diagnostics
class DiagnosisSeverity(StrEnum):
    """How much attention this endpoint's state deserves right now.

    Wider than the alert `Severity` on purpose: an alert only has to decide
    whether to wake someone, whereas a diagnosis has to rank a queue of
    endpoints an engineer is working through.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


DIAGNOSIS_SEVERITY_ORDER: dict[str, int] = {
    DiagnosisSeverity.INFO: 0,
    DiagnosisSeverity.LOW: 1,
    DiagnosisSeverity.MEDIUM: 2,
    DiagnosisSeverity.HIGH: 3,
    DiagnosisSeverity.CRITICAL: 4,
}


class Confidence(StrEnum):
    """How well the evidence supports a conclusion.

    Deliberately three coarse bands. A precise-looking percentage on a
    handful of heuristics would imply a rigour that is not there.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceKind(StrEnum):
    """Where a statement came from - the guard against inventing facts.

    OBSERVED is something this diagnosis actually measured or read from the
    database. INFERRED is a conclusion drawn from observations. UNKNOWN is a
    thing that matters but that InfraSight cannot see from where it runs,
    and is stated as such rather than guessed at.
    """

    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class ActionRisk(StrEnum):
    """How dangerous a recommended action is in production.

    SAFE is read-only. DISRUPTIVE interrupts service briefly. HIGH_RISK can
    lose data or take an application down; nothing in that band is ever
    executed by InfraSight, only described.
    """

    SAFE = "safe"
    DISRUPTIVE = "disruptive"
    HIGH_RISK = "high_risk"


class DiagnosisFocus(StrEnum):
    """Which question the operator is asking."""

    AUTO = "auto"
    ENDPOINT = "endpoint"
    SSL = "ssl"
    AVAILABILITY = "availability"
    PERFORMANCE = "performance"
    RECENT_FAILURE = "recent_failure"
    DEPLOYMENT_IMPACT = "deployment_impact"


# --------------------------------------------------------------- RCA
class RcaStatus(StrEnum):
    """Where a root-cause analysis has got to.

    Deliberately parallel to, and independent of, the incident lifecycle. An
    incident can be closed while its RCA is still pending - that combination
    is normal and valid, and forcing them to move together is what makes RCA
    processes get skipped.
    """

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"


OPEN_RCA_STATUSES = frozenset({RcaStatus.PENDING.value, RcaStatus.IN_PROGRESS.value})


class RcaOwnerType(StrEnum):
    """An RCA belongs to one person or to a team.

    Teams are the free-text labels already used on endpoints rather than a new
    entity - the application does not need a team table to answer "who owns
    this", and adding one would be a migration and a management screen for no
    extra capability.
    """

    INDIVIDUAL = "individual"
    TEAM = "team"


class RootCauseCategory(StrEnum):
    """Optional classification, so recurring causes can be counted.

    The value of a category is entirely in aggregation - "34% of our outages
    are deployment-related" is actionable in a way that thirty individual
    write-ups are not.
    """

    APPLICATION = "application"
    INFRASTRUCTURE = "infrastructure"
    NETWORK = "network"
    DATABASE = "database"
    DEPLOYMENT = "deployment"
    CONFIGURATION = "configuration"
    SSL_TLS = "ssl_tls"
    SECURITY = "security"
    DEPENDENCY = "dependency"
    HUMAN_ERROR = "human_error"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"
