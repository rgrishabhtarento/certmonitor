"""Health-path discovery.

Adds ``endpoints.resolved_health_path``: the path that actually answered when
the configured one turned out not to exist. Storing it means the 404 and the
search are paid once rather than on every check interval, and the operator's
own ``url`` is never rewritten behind their back.

Revision ID: 0003
Revises: 0002
Created: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "endpoints",
        sa.Column("resolved_health_path", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("endpoints", "resolved_health_path")
