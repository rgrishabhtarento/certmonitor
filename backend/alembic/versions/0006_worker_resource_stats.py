"""Worker resource reporting.

Three nullable columns on ``worker_heartbeats``. The worker measures its own
cgroup CPU and memory and writes the numbers on the heartbeat it already
sends, which lets the API report worker resource use without a channel to the
worker process - and, more to the point, without mounting the Docker socket
into a network-facing container.

Revision ID: 0006
Revises: 0005
Created: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_heartbeats", sa.Column("cpu_percent", sa.Float(), nullable=True)
    )
    op.add_column(
        "worker_heartbeats", sa.Column("memory_mb", sa.Float(), nullable=True)
    )
    op.add_column(
        "worker_heartbeats", sa.Column("memory_limit_mb", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("worker_heartbeats", "memory_limit_mb")
    op.drop_column("worker_heartbeats", "memory_mb")
    op.drop_column("worker_heartbeats", "cpu_percent")
