"""Diagnosis history.

Stores the conclusion of each diagnosis so the engine can answer "is this the
fourth time this month?" - the question that separates a capacity problem from
four unrelated outages. Raw probe payloads are deliberately not stored: they
are large, stale within minutes, and would grow this table without bound.

Revision ID: 0004
Revises: 0003
Created: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TZ = sa.DateTime(timezone=True)
UUID = sa.Uuid(as_uuid=True)
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "diagnoses",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False),
        sa.Column("endpoint_id", UUID, nullable=False),
        sa.Column("endpoint_name", sa.String(160), nullable=True),
        sa.Column("application", sa.String(128), nullable=True),
        sa.Column("requested_by_id", UUID, nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("focus", sa.String(24), nullable=False, server_default="auto"),
        sa.Column("verdict", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("endpoint_status", sa.String(16), nullable=True),
        sa.Column("deepest_layer_ok", sa.String(8), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.Float(), nullable=True),
        sa.Column("candidates", JSONB, nullable=True),
        sa.Column("actions", JSONB, nullable=True),
        sa.Column("incident_id", BIGINT, nullable=True),
        sa.Column("change_id", BIGINT, nullable=True),
        # Filled in by an operator afterwards: what actually fixed it.
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"],
            name="fk_diagnoses_endpoint_id_endpoints", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["users.id"],
            name="fk_diagnoses_requested_by_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_diagnoses"),
    )
    op.create_index(
        "ix_diagnoses_endpoint_time", "diagnoses", ["endpoint_id", "created_at"]
    )
    op.create_index("ix_diagnoses_verdict", "diagnoses", ["verdict"])
    op.create_index("ix_diagnoses_severity", "diagnoses", ["severity"])
    op.create_index("ix_diagnoses_application", "diagnoses", ["application"])
    op.create_index("ix_diagnoses_created_at", "diagnoses", ["created_at"])


def downgrade() -> None:
    for index in (
        "ix_diagnoses_created_at",
        "ix_diagnoses_application",
        "ix_diagnoses_severity",
        "ix_diagnoses_verdict",
        "ix_diagnoses_endpoint_time",
    ):
        op.drop_index(index, table_name="diagnoses")
    op.drop_table("diagnoses")
