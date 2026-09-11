"""Link attachments on an RCA.

A URL field, not a file store: an RCA can point at wherever the actual
document already lives (Drive, OneDrive, SharePoint, an internal wiki page),
rather than InfraSight hosting the file itself.

Revision ID: 0012
Revises: 0011
Created: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column("rcas", sa.Column("attachments", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("rcas", "attachments")
