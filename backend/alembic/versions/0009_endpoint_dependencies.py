"""Declared dependencies between endpoints.

Lets an endpoint name another monitored endpoint as something it depends on
(its database's health-check proxy, a shared auth service, and so on) so
Diagnose can correlate "this is down AND its declared dependency is also
down" as evidence, without any cluster or protocol-specific access - a
dependency here is just another endpoint InfraSight already monitors.

Revision ID: 0009
Revises: 0008
Created: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endpoint_dependencies",
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_endpoint_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"],
            name=op.f("fk_endpoint_dependencies_endpoint_id_endpoints"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_endpoint_id"], ["endpoints.id"],
            name=op.f("fk_endpoint_dependencies_depends_on_endpoint_id_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "endpoint_id", "depends_on_endpoint_id",
            name=op.f("pk_endpoint_dependencies"),
        ),
        sa.CheckConstraint(
            "endpoint_id <> depends_on_endpoint_id",
            name="ck_endpoint_dependencies_not_self",
        ),
    )
    op.create_index(
        "ix_endpoint_dependencies_depends_on",
        "endpoint_dependencies",
        ["depends_on_endpoint_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_endpoint_dependencies_depends_on", table_name="endpoint_dependencies"
    )
    op.drop_table("endpoint_dependencies")
