"""Uploaded logo, and drop the emoji experiment.

Replaces the emoji branding and avatars added in 0007 with a real uploaded
logo. 0007 is left untouched rather than rewritten: it may already have run,
and an applied migration must never change under a deployment's feet.

The logo lives in a table rather than on a volume so it survives a container
rebuild and is identical for every replica. Feature flags need no schema -
they are ordinary rows in ``system_settings``, seeded by the bootstrap.

Revision ID: 0008
Revises: 0007
Created: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "branding_assets",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_branding_assets_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_branding_assets")),
    )

    op.drop_column("users", "avatar_emoji")

    # The emoji logo setting is gone from the specs, so its row would sit
    # orphaned. load_settings ignores unknown keys, but leaving it would show
    # up in a database dump as a setting that no longer exists.
    op.execute("DELETE FROM system_settings WHERE key = 'branding_logo_text'")


def downgrade() -> None:
    op.add_column("users", sa.Column("avatar_emoji", sa.String(length=32), nullable=True))
    op.drop_table("branding_assets")
