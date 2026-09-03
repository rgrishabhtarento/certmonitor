"""Change management.

Adds the change request, its comments and its activity timeline, the link
table to affected endpoints, and the two endpoint columns that let a
deployment pause be attributed and safely undone.

``change_endpoints.was_paused_before`` is the important one: without it,
completing a deployment would resume monitoring on an endpoint an operator had
deliberately paused for their own reasons.

Revision ID: 0002
Revises: 0001
Created: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TZ = sa.DateTime(timezone=True)
UUID = sa.Uuid(as_uuid=True)
# Matches the models: BIGINT on PostgreSQL, INTEGER on SQLite so the test
# suite gets a working autoincrement.
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "changes",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        # Human-facing identifier, e.g. CHG-2026-0001.
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("application", sa.String(128), nullable=False),
        sa.Column("environment_id", UUID, nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expected_start_at", TZ, nullable=False),
        sa.Column("expected_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("risk", sa.String(8), nullable=False),
        sa.Column("rollback_plan", sa.Text(), nullable=True),
        sa.Column("deployment_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requester_id", UUID, nullable=True),
        # Names are denormalised so the record still reads correctly after the
        # user row is deleted.
        sa.Column("requester_name", sa.String(64), nullable=True),
        sa.Column("approver_id", UUID, nullable=True),
        sa.Column("approver_name", sa.String(64), nullable=True),
        sa.Column("approved_at", TZ, nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("deployer_id", UUID, nullable=True),
        sa.Column("deployer_name", sa.String(64), nullable=True),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("completed_at", TZ, nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("health_check", JSONB, nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"],
            name="fk_changes_environment_id_environments", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"], ["users.id"],
            name="fk_changes_requester_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"], ["users.id"],
            name="fk_changes_approver_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deployer_id"], ["users.id"],
            name="fk_changes_deployer_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_changes"),
        sa.UniqueConstraint("reference", name="uq_changes_reference"),
    )
    op.create_index("ix_changes_status", "changes", ["status"])
    op.create_index("ix_changes_application", "changes", ["application"])
    op.create_index("ix_changes_environment_id", "changes", ["environment_id"])
    op.create_index("ix_changes_expected_start", "changes", ["expected_start_at"])
    op.create_index("ix_changes_app_env", "changes", ["application", "environment_id"])
    op.create_index("ix_changes_requester", "changes", ["requester_id"])

    op.create_table(
        "change_endpoints",
        sa.Column("change_id", BIGINT, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=False),
        # Whether the endpoint was already paused when the deployment started.
        sa.Column(
            "was_paused_before", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["changes.id"],
            name="fk_change_endpoints_change_id_changes", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"],
            name="fk_change_endpoints_endpoint_id_endpoints", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("change_id", "endpoint_id", name="pk_change_endpoints"),
    )

    op.create_table(
        "change_comments",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("change_id", BIGINT, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_id"], ["changes.id"],
            name="fk_change_comments_change_id_changes", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_change_comments_user_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_change_comments"),
    )
    op.create_index(
        "ix_change_comments_change", "change_comments", ["change_id", "created_at"]
    )

    op.create_table(
        "change_activity",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("change_id", BIGINT, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_id"], ["changes.id"],
            name="fk_change_activity_change_id_changes", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_change_activity_user_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_change_activity"),
    )
    op.create_index(
        "ix_change_activity_change", "change_activity", ["change_id", "created_at"]
    )

    # --- endpoints: attribute the pause ---------------------------------
    op.add_column("endpoints", sa.Column("pause_reason", sa.String(255), nullable=True))
    op.add_column(
        "endpoints", sa.Column("paused_by_change_id", BIGINT, nullable=True)
    )
    op.create_foreign_key(
        "fk_endpoints_paused_by_change_id_changes",
        "endpoints", "changes",
        ["paused_by_change_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_endpoints_paused_by_change_id", "endpoints", ["paused_by_change_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_endpoints_paused_by_change_id", table_name="endpoints")
    op.drop_constraint(
        "fk_endpoints_paused_by_change_id_changes", "endpoints", type_="foreignkey"
    )
    op.drop_column("endpoints", "paused_by_change_id")
    op.drop_column("endpoints", "pause_reason")

    op.drop_index("ix_change_activity_change", table_name="change_activity")
    op.drop_table("change_activity")
    op.drop_index("ix_change_comments_change", table_name="change_comments")
    op.drop_table("change_comments")
    op.drop_table("change_endpoints")

    for index in (
        "ix_changes_requester",
        "ix_changes_app_env",
        "ix_changes_expected_start",
        "ix_changes_environment_id",
        "ix_changes_application",
        "ix_changes_status",
    ):
        op.drop_index(index, table_name="changes")
    op.drop_table("changes")
