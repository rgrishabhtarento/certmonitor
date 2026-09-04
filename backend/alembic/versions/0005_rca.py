"""Lightweight RCA management.

Adds ``rcas`` (one optional root-cause analysis per incident), incident
comments, and a free-text ``team`` label on users.

The team column is deliberately a string rather than a table. RCA ownership
needs to answer "is this mine?", and a label already used the same way on
endpoints answers it without a membership model, a management screen or a new
role.

Revision ID: 0005
Revises: 0004
Created: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TZ = sa.DateTime(timezone=True)
UUID = sa.Uuid(as_uuid=True)
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.add_column("users", sa.Column("team", sa.String(64), nullable=True))
    op.create_index("ix_users_team", "users", ["team"])

    op.create_table(
        "rcas",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("incident_id", BIGINT, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=True),
        sa.Column("endpoint_name", sa.String(160), nullable=True),
        sa.Column("application", sa.String(128), nullable=True),
        sa.Column("environment", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        # --- ownership: a person or a team, never both
        sa.Column("owner_type", sa.String(12), nullable=True),
        sa.Column("owner_user_id", UUID, nullable=True),
        sa.Column("owner_user_name", sa.String(64), nullable=True),
        sa.Column("owner_team", sa.String(64), nullable=True),
        sa.Column("requested_by_id", UUID, nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("requested_at", TZ, nullable=True),
        sa.Column("due_at", TZ, nullable=True),
        # --- content
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("root_cause_category", sa.String(24), nullable=True),
        sa.Column("impact", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("preventive_actions", JSONB, nullable=True),
        sa.Column("timeline", JSONB, nullable=True),
        sa.Column("diagnosis_id", BIGINT, nullable=True),
        sa.Column("change_id", BIGINT, nullable=True),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("completed_at", TZ, nullable=True),
        sa.Column("completed_by", sa.String(64), nullable=True),
        sa.Column("not_required_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"],
            name="fk_rcas_incident_id_incidents", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"],
            name="fk_rcas_endpoint_id_endpoints", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"],
            name="fk_rcas_owner_user_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["users.id"],
            name="fk_rcas_requested_by_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rcas"),
        # One RCA per incident - a double-click cannot create two.
        sa.UniqueConstraint("incident_id", name="uq_rcas_incident_id"),
    )
    op.create_index("ix_rcas_status", "rcas", ["status"])
    op.create_index("ix_rcas_status_created", "rcas", ["status", "created_at"])
    op.create_index("ix_rcas_owner_user", "rcas", ["owner_user_id"])
    op.create_index("ix_rcas_owner_team", "rcas", ["owner_team"])
    op.create_index("ix_rcas_category", "rcas", ["root_cause_category"])
    op.create_index("ix_rcas_application", "rcas", ["application"])
    op.create_index("ix_rcas_environment", "rcas", ["environment"])
    op.create_index("ix_rcas_created_at", "rcas", ["created_at"])

    op.create_table(
        "incident_comments",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("incident_id", BIGINT, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"],
            name="fk_incident_comments_incident_id_incidents", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_incident_comments_user_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incident_comments"),
    )
    op.create_index(
        "ix_incident_comments_incident", "incident_comments",
        ["incident_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_incident_comments_incident", table_name="incident_comments")
    op.drop_table("incident_comments")

    for index in (
        "ix_rcas_created_at",
        "ix_rcas_environment",
        "ix_rcas_application",
        "ix_rcas_category",
        "ix_rcas_owner_team",
        "ix_rcas_owner_user",
        "ix_rcas_status_created",
        "ix_rcas_status",
    ):
        op.drop_index(index, table_name="rcas")
    op.drop_table("rcas")

    op.drop_index("ix_users_team", table_name="users")
    op.drop_column("users", "team")
