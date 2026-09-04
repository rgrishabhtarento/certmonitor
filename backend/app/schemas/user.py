"""User management schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.enums import RoleName
from app.schemas.common import ORMModel


class UserRead(ORMModel):
    id: str
    username: str
    email: str | None = None
    full_name: str | None = None
    # Free-text team label, used for RCA ownership.
    team: str | None = None
    role: str
    permissions: list[str] = Field(default_factory=list)
    is_active: bool
    must_change_password: bool
    is_locked: bool = False
    locked_until: datetime | None = None
    failed_login_attempts: int = 0
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    password_changed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default=RoleName.VIEWER.value)
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=128)
    team: str | None = Field(
        default=None, max_length=64,
        description="Free-text team label, e.g. DevOps. Used for RCA ownership.",
    )
    is_active: bool = True
    # New accounts start with a temporary password chosen by an admin, so the
    # user must replace it before doing anything else.
    must_change_password: bool = True

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        value = value.strip()
        if not all(ch.isalnum() or ch in "._-" for ch in value):
            raise ValueError(
                "username may contain only letters, digits, dots, underscores "
                "and hyphens"
            )
        return value

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {r.value for r in RoleName}:
            raise ValueError(
                "role must be one of: " + ", ".join(r.value for r in RoleName)
            )
        return value


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=128)
    team: str | None = Field(
        default=None, max_length=64,
        description="Free-text team label, e.g. DevOps. Used for RCA ownership.",
    )
    role: str | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None
    unlock: bool | None = Field(
        default=None, description="Clear a brute-force lockout on this account."
    )

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in {r.value for r in RoleName}:
            raise ValueError(
                "role must be one of: " + ", ".join(r.value for r in RoleName)
            )
        return value


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)
    force_change: bool = Field(
        default=True,
        description="Require the user to choose a new password at next sign-in.",
    )


class RoleRead(ORMModel):
    id: str
    name: str
    description: str | None = None
    is_system: bool
    permissions: list[str] = Field(default_factory=list)
    user_count: int = 0
