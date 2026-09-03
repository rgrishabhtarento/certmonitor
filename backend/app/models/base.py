"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# Explicit, predictable constraint names keep Alembic autogenerate diffs clean.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on PostgreSQL (indexable, binary) and plain JSON elsewhere so the test
# suite can run on SQLite.
JSONType = JSON().with_variant(JSONB, "postgresql")

# Always store timezone-aware UTC timestamps.
TimestampTZ = DateTime(timezone=True)

# SQLite has no autoincrementing BIGINT - only INTEGER PRIMARY KEY - so the
# high-volume tables map to INTEGER there and BIGINT on PostgreSQL. Without
# this, the test suite could not insert monitoring results.
BigIntType = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TimestampTZ,
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
