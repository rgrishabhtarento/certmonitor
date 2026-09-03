"""Initial schema.

Creates every table the application needs, with the indexes the hot queries
depend on:

* ``ix_endpoints_due`` serves the worker's claim query.
* ``ix_monitoring_results_endpoint_time`` serves history and statistics.
* ``uq_incidents_one_open_per_endpoint`` is a partial unique index that
  enforces at most one open incident per endpoint at the database level - the
  guarantee behind "four consecutive failures are one incident, not four".

Revision ID: 0001
Revises: None
Created: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB where available, JSON elsewhere - the test suite runs on SQLite.
JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TZ = sa.DateTime(timezone=True)
UUID = sa.Uuid(as_uuid=True)


def upgrade() -> None:
    # ------------------------------------------------------- permissions
    op.create_table(
        "permissions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_permissions"),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    op.create_table(
        "roles",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_index("ix_roles_created_at", "roles", ["created_at"])

    op.create_table(
        "role_permissions",
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("permission_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_role_permissions_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name="fk_role_permissions_permission_id_permissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permissions"),
    )

    # -------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("full_name", sa.String(128), nullable=True),
        # bcrypt digest only; plaintext never reaches the database.
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", TZ, nullable=True),
        sa.Column("last_login_ip", sa.String(64), nullable=True),
        sa.Column("password_changed_at", TZ, nullable=True),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", TZ, nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("created_by_id", UUID, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_users_role_id_roles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_users_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_role_id", "users", ["role_id"])
    op.create_index("ix_users_created_at", "users", ["created_at"])

    # ------------------------------------------------- environments & tags
    op.create_table(
        "environments",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_environments"),
        sa.UniqueConstraint("name", name="uq_environments_name"),
    )
    op.create_index("ix_environments_created_at", "environments", ["created_at"])

    op.create_table(
        "tags",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.UniqueConstraint("name", name="uq_tags_name"),
    )
    op.create_index("ix_tags_created_at", "tags", ["created_at"])

    # ---------------------------------------------------------- endpoints
    op.create_table(
        "endpoints",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("check_type", sa.String(16), nullable=False),
        sa.Column("http_method", sa.String(10), nullable=False),
        sa.Column("environment_id", UUID, nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("team", sa.String(128), nullable=True),
        sa.Column("application", sa.String(128), nullable=True),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_paused", sa.Boolean(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("expected_status_codes", sa.String(128), nullable=False),
        sa.Column("expected_body_substring", sa.String(255), nullable=True),
        sa.Column("follow_redirects", sa.Boolean(), nullable=False),
        sa.Column("verify_ssl", sa.Boolean(), nullable=False),
        sa.Column("ssl_monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("custom_headers", JSONB, nullable=True),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("auth_username", sa.String(128), nullable=True),
        sa.Column("auth_header_name", sa.String(128), nullable=True),
        # Fernet ciphertext. Never returned by the API in any form.
        sa.Column("auth_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("auth_secret_hint", sa.String(64), nullable=True),
        sa.Column("failure_threshold", sa.Integer(), nullable=False),
        sa.Column("response_time_threshold_ms", sa.Integer(), nullable=True),
        sa.Column("ssl_warning_days", sa.Integer(), nullable=True),
        sa.Column("ssl_critical_days", sa.Integer(), nullable=True),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False),
        sa.Column("current_status", sa.String(16), nullable=False),
        sa.Column("last_checked_at", TZ, nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_response_time_ms", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("consecutive_successes", sa.Integer(), nullable=False),
        sa.Column("total_checks", sa.Integer(), nullable=False),
        sa.Column("total_failures", sa.Integer(), nullable=False),
        sa.Column("ssl_status", sa.String(24), nullable=False),
        sa.Column("ssl_expires_at", TZ, nullable=True),
        sa.Column("ssl_days_remaining", sa.Integer(), nullable=True),
        sa.Column("ssl_issuer", sa.String(255), nullable=True),
        sa.Column("ssl_common_name", sa.String(255), nullable=True),
        sa.Column("next_check_at", TZ, nullable=True),
        sa.Column("lease_expires_at", TZ, nullable=True),
        sa.Column("leased_by", sa.String(64), nullable=True),
        sa.Column("created_by_id", UUID, nullable=True),
        sa.Column("updated_by_id", UUID, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name="fk_endpoints_environment_id_environments",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_endpoints_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_endpoints_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_endpoints"),
    )
    op.create_index("ix_endpoints_name", "endpoints", ["name"])
    op.create_index("ix_endpoints_hostname", "endpoints", ["hostname"])
    op.create_index("ix_endpoints_owner", "endpoints", ["owner"])
    op.create_index("ix_endpoints_team", "endpoints", ["team"])
    op.create_index("ix_endpoints_application", "endpoints", ["application"])
    op.create_index("ix_endpoints_environment_id", "endpoints", ["environment_id"])
    op.create_index("ix_endpoints_current_status", "endpoints", ["current_status"])
    op.create_index("ix_endpoints_next_check_at", "endpoints", ["next_check_at"])
    op.create_index("ix_endpoints_created_at", "endpoints", ["created_at"])
    # The worker's claim query: enabled, not paused, ordered by due time.
    op.create_index(
        "ix_endpoints_due",
        "endpoints",
        ["monitoring_enabled", "is_paused", "next_check_at"],
    )
    op.create_index(
        "ix_endpoints_status_env", "endpoints", ["current_status", "environment_id"]
    )
    op.create_index("ix_endpoints_ssl_expiry", "endpoints", ["ssl_expires_at"])

    op.create_table(
        "endpoint_tags",
        sa.Column("endpoint_id", UUID, nullable=False),
        sa.Column("tag_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_endpoint_tags_endpoint_id_endpoints",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name="fk_endpoint_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("endpoint_id", "tag_id", name="pk_endpoint_tags"),
    )

    # -------------------------------------------------- monitoring results
    op.create_table(
        "monitoring_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=False),
        sa.Column("checked_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.Float(), nullable=True),
        sa.Column("dns_time_ms", sa.Float(), nullable=True),
        sa.Column("connect_time_ms", sa.Float(), nullable=True),
        sa.Column("tls_time_ms", sa.Float(), nullable=True),
        sa.Column("ttfb_ms", sa.Float(), nullable=True),
        sa.Column("total_time_ms", sa.Float(), nullable=True),
        sa.Column("resolved_ip", sa.String(64), nullable=True),
        sa.Column("content_length", sa.Integer(), nullable=True),
        sa.Column("redirect_count", sa.Integer(), nullable=False),
        sa.Column("final_url", sa.String(2048), nullable=True),
        sa.Column("redirect_chain", JSONB, nullable=True),
        sa.Column("response_headers", JSONB, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.String(32), nullable=False),
        sa.Column("tls_version", sa.String(24), nullable=True),
        sa.Column("tls_cipher", sa.String(96), nullable=True),
        sa.Column("cert_expires_at", TZ, nullable=True),
        sa.Column("ssl_days_remaining", sa.Integer(), nullable=True),
        sa.Column("ssl_status", sa.String(24), nullable=True),
        sa.Column("checked_by", sa.String(64), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_monitoring_results_endpoint_id_endpoints",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitoring_results"),
    )
    # Covers the history query, the statistics aggregates and the retention
    # sweep's range delete.
    op.create_index(
        "ix_monitoring_results_endpoint_time",
        "monitoring_results",
        ["endpoint_id", "checked_at"],
    )
    op.create_index(
        "ix_monitoring_results_checked_at", "monitoring_results", ["checked_at"]
    )
    op.create_index(
        "ix_monitoring_results_endpoint_status",
        "monitoring_results",
        ["endpoint_id", "status"],
    )

    # ---------------------------------------------------- ssl certificates
    op.create_table(
        "ssl_certificates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=False),
        sa.Column("fingerprint_sha256", sa.String(95), nullable=True),
        sa.Column("serial_number", sa.String(128), nullable=True),
        sa.Column("subject", sa.String(512), nullable=True),
        sa.Column("common_name", sa.String(255), nullable=True),
        sa.Column("issuer", sa.String(512), nullable=True),
        sa.Column("issuer_common_name", sa.String(255), nullable=True),
        sa.Column("issuer_organization", sa.String(255), nullable=True),
        sa.Column("san", JSONB, nullable=True),
        sa.Column("valid_from", TZ, nullable=True),
        sa.Column("valid_to", TZ, nullable=True),
        sa.Column("days_remaining", sa.Integer(), nullable=True),
        sa.Column("signature_algorithm", sa.String(96), nullable=True),
        sa.Column("key_algorithm", sa.String(48), nullable=True),
        sa.Column("key_size", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(16), nullable=True),
        sa.Column("tls_version", sa.String(24), nullable=True),
        sa.Column("tls_cipher", sa.String(96), nullable=True),
        sa.Column("is_self_signed", sa.Boolean(), nullable=False),
        sa.Column("is_wildcard", sa.Boolean(), nullable=False),
        sa.Column("hostname_matches", sa.Boolean(), nullable=True),
        sa.Column("chain_verified", sa.Boolean(), nullable=True),
        sa.Column("verification_status", sa.String(32), nullable=True),
        sa.Column("verification_error", sa.Text(), nullable=True),
        sa.Column("chain", JSONB, nullable=True),
        sa.Column("chain_length", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("checked_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_ssl_certificates_endpoint_id_endpoints",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ssl_certificates"),
    )
    op.create_index(
        "ix_ssl_certificates_fingerprint_sha256",
        "ssl_certificates",
        ["fingerprint_sha256"],
    )
    op.create_index(
        "ix_ssl_certificates_common_name", "ssl_certificates", ["common_name"]
    )
    op.create_index(
        "ix_ssl_certificates_issuer_common_name",
        "ssl_certificates",
        ["issuer_common_name"],
    )
    op.create_index(
        "ix_ssl_certificates_endpoint_current",
        "ssl_certificates",
        ["endpoint_id", "is_current"],
    )
    op.create_index("ix_ssl_certificates_valid_to", "ssl_certificates", ["valid_to"])
    op.create_index("ix_ssl_certificates_status", "ssl_certificates", ["status"])

    # ---------------------------------------------------------- incidents
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("started_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("first_failure_status_code", sa.Integer(), nullable=True),
        sa.Column("failed_check_count", sa.Integer(), nullable=False),
        sa.Column("recovery_status_code", sa.Integer(), nullable=True),
        sa.Column("recovery_response_time_ms", sa.Float(), nullable=True),
        sa.Column("timeline", JSONB, nullable=True),
        sa.Column("acknowledged_by_id", UUID, nullable=True),
        sa.Column("acknowledged_at", TZ, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_incidents_endpoint_id_endpoints",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_id"],
            ["users.id"],
            name="fk_incidents_acknowledged_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
    )
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index(
        "ix_incidents_endpoint_status", "incidents", ["endpoint_id", "status"]
    )
    op.create_index("ix_incidents_started_at", "incidents", ["started_at"])
    # Partial unique index: at most one OPEN incident per endpoint. This is
    # what makes a run of consecutive failures collapse into a single
    # incident even with several workers racing.
    op.create_index(
        "uq_incidents_one_open_per_endpoint",
        "incidents",
        ["endpoint_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )

    # ------------------------------------------------------------- alerts
    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=True),
        sa.Column("incident_id", sa.BigInteger(), nullable=True),
        sa.Column("alert_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("is_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("acknowledged_by_id", UUID, nullable=True),
        sa.Column("acknowledged_at", TZ, nullable=True),
        sa.Column("notification_status", sa.String(16), nullable=False),
        sa.Column("notification_attempts", sa.Integer(), nullable=False),
        sa.Column("notification_error", sa.Text(), nullable=True),
        sa.Column("notified_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_alerts_endpoint_id_endpoints",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_alerts_incident_id_incidents",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_id"],
            ["users.id"],
            name="fk_alerts_acknowledged_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
    )
    op.create_index("ix_alerts_endpoint_id", "alerts", ["endpoint_id"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_index("ix_alerts_endpoint_type", "alerts", ["endpoint_id", "alert_type"])
    op.create_index("ix_alerts_ack", "alerts", ["is_acknowledged", "severity"])

    # ---------------------------------------------- notification channels
    op.create_table(
        "notification_channels",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(96), nullable=False),
        sa.Column("channel_type", sa.String(24), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        # Whole provider config as one Fernet blob: webhook URLs, SMTP
        # passwords and routing keys are never stored in the clear.
        sa.Column("config_encrypted", sa.Text(), nullable=True),
        sa.Column("config_public", JSONB, nullable=True),
        sa.Column("min_severity", sa.String(16), nullable=False),
        sa.Column("event_types", JSONB, nullable=True),
        sa.Column("environment_filter", JSONB, nullable=True),
        sa.Column("tag_filter", JSONB, nullable=True),
        sa.Column("last_used_at", TZ, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("created_by_id", UUID, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_notification_channels_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_channels"),
        sa.UniqueConstraint("name", name="uq_notification_channels_name"),
    )
    op.create_index(
        "ix_notification_channels_created_at", "notification_channels", ["created_at"]
    )

    # --------------------------------------------------------- audit logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        # Denormalised so the trail survives deletion of the acting user.
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("resource_name", sa.String(255), nullable=True),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("request_method", sa.String(10), nullable=True),
        sa.Column("request_path", sa.String(512), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_audit_logs_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_username", "audit_logs", ["username"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_user_action", "audit_logs", ["user_id", "action"])
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"]
    )

    # ----------------------------------------------------- system settings
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("allowed_values", JSONB, nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("is_editable", sa.Boolean(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by_id", UUID, nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name="fk_system_settings_updated_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )
    op.create_index("ix_system_settings_category", "system_settings", ["category"])

    # ------------------------------------------------- worker heartbeats
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("last_seen_at", TZ, nullable=False),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("checks_completed", sa.BigInteger(), nullable=False),
        sa.Column("checks_failed", sa.BigInteger(), nullable=False),
        sa.Column("in_flight", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(32), nullable=True),
        sa.Column("hostname", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_heartbeats"),
    )


def downgrade() -> None:
    # Reverse dependency order.
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_system_settings_category", table_name="system_settings")
    op.drop_table("system_settings")

    for index in (
        "ix_audit_logs_resource",
        "ix_audit_logs_user_action",
        "ix_audit_logs_created_at",
        "ix_audit_logs_action",
        "ix_audit_logs_username",
    ):
        op.drop_index(index, table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index(
        "ix_notification_channels_created_at", table_name="notification_channels"
    )
    op.drop_table("notification_channels")

    for index in (
        "ix_alerts_ack",
        "ix_alerts_endpoint_type",
        "ix_alerts_created_at",
        "ix_alerts_endpoint_id",
    ):
        op.drop_index(index, table_name="alerts")
    op.drop_table("alerts")

    for index in (
        "uq_incidents_one_open_per_endpoint",
        "ix_incidents_started_at",
        "ix_incidents_endpoint_status",
        "ix_incidents_status",
    ):
        op.drop_index(index, table_name="incidents")
    op.drop_table("incidents")

    for index in (
        "ix_ssl_certificates_status",
        "ix_ssl_certificates_valid_to",
        "ix_ssl_certificates_endpoint_current",
        "ix_ssl_certificates_issuer_common_name",
        "ix_ssl_certificates_common_name",
        "ix_ssl_certificates_fingerprint_sha256",
    ):
        op.drop_index(index, table_name="ssl_certificates")
    op.drop_table("ssl_certificates")

    for index in (
        "ix_monitoring_results_endpoint_status",
        "ix_monitoring_results_checked_at",
        "ix_monitoring_results_endpoint_time",
    ):
        op.drop_index(index, table_name="monitoring_results")
    op.drop_table("monitoring_results")

    op.drop_table("endpoint_tags")

    for index in (
        "ix_endpoints_ssl_expiry",
        "ix_endpoints_status_env",
        "ix_endpoints_due",
        "ix_endpoints_created_at",
        "ix_endpoints_next_check_at",
        "ix_endpoints_current_status",
        "ix_endpoints_environment_id",
        "ix_endpoints_application",
        "ix_endpoints_team",
        "ix_endpoints_owner",
        "ix_endpoints_hostname",
        "ix_endpoints_name",
    ):
        op.drop_index(index, table_name="endpoints")
    op.drop_table("endpoints")

    op.drop_index("ix_tags_created_at", table_name="tags")
    op.drop_table("tags")
    op.drop_index("ix_environments_created_at", table_name="environments")
    op.drop_table("environments")

    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_index("ix_users_role_id", table_name="users")
    op.drop_table("users")

    op.drop_table("role_permissions")
    op.drop_index("ix_roles_created_at", table_name="roles")
    op.drop_table("roles")
    op.drop_table("permissions")
