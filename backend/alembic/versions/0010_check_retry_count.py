"""Record how many retries a check took.

Supports the new opt-in ``check_retry_attempts`` setting (default 0, off).
Kept as its own column rather than folded into an existing one so a
success-after-retry stays distinguishable from a clean first-attempt pass -
without it, retries would quietly erase the signal the intermittent-failure
detector is built to catch.

Revision ID: 0010
Revises: 0009
Created: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitoring_results",
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("monitoring_results", "retry_count")
