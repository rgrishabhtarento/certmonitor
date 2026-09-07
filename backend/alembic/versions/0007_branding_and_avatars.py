"""User avatar emoji.

One nullable column on ``users``. The branding half of this feature needs no
schema change: app name and logo text are ordinary rows in
``system_settings``, seeded by the bootstrap like every other setting.

Revision ID: 0007
Revises: 0006
Created: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_emoji", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_emoji")
