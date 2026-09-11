"""Environment-level threshold overrides.

Adds a middle tier between a per-endpoint override and the global runtime
setting: an environment (e.g. "staging") can set its own failure threshold,
SSL warning/critical days and response-time threshold. NULL means "inherit",
so existing environments and endpoints behave exactly as before until
someone opts an environment in. Resolution order: endpoint override ->
environment override -> global setting (monitoring_service.resolve_thresholds).

Revision ID: 0011
Revises: 0010
Created: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("environments", sa.Column("failure_threshold", sa.Integer(), nullable=True))
    op.add_column("environments", sa.Column("ssl_warning_days", sa.Integer(), nullable=True))
    op.add_column("environments", sa.Column("ssl_critical_days", sa.Integer(), nullable=True))
    op.add_column(
        "environments",
        sa.Column("response_time_threshold_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("environments", "response_time_threshold_ms")
    op.drop_column("environments", "ssl_critical_days")
    op.drop_column("environments", "ssl_warning_days")
    op.drop_column("environments", "failure_threshold")
