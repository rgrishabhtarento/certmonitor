"""Users, roles and permissions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Table, Column, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    TimestampMixin,
    TimestampTZ,
    UUIDPrimaryKeyMixin,
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id",
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        Uuid(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Permission(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))

    roles: Mapped[list["Role"]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions,
        back_populates="roles",
        lazy="selectin",
    )
    users: Mapped[list["User"]] = relationship(back_populates="role")

    @property
    def permission_codes(self) -> set[str]:
        return {p.code for p in self.permissions}


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    # Free-text team label, matching the convention already used on endpoints.
    # It is what lets an RCA be owned by "DevOps" rather than by a person, and
    # it needs no team table, membership screen or extra role to do that.
    team: Mapped[str | None] = mapped_column(String(64), index=True)

    # Only ever a bcrypt digest; the plaintext never leaves the request scope.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    role: Mapped[Role] = relationship(back_populates="users", lazy="joined")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    last_login_at: Mapped[datetime | None] = mapped_column(TimestampTZ)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    password_changed_at: Mapped[datetime | None] = mapped_column(TimestampTZ)

    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    locked_until: Mapped[datetime | None] = mapped_column(TimestampTZ)

    # Bumped on password change / forced logout so previously issued JWTs stop
    # validating without needing a server-side session store.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def role_name(self) -> str:
        return self.role.name if self.role else ""

    @property
    def permissions(self) -> set[str]:
        return self.role.permission_codes if self.role else set()

    def has_permission(self, code: str) -> bool:
        return code in self.permissions
