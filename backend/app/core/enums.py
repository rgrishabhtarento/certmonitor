"""Domain enumerations.

These are stored as short strings in PostgreSQL rather than native ENUM types:
adding a value later is then a code change instead of a migration that has to
mutate a type under load.
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    ADMIN = "admin"
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


ROLE_PERMISSIONS: dict[str, set[str]] = {
    RoleName.ADMIN: {p.value for p in Permission},
    RoleName.VIEWER: {
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
