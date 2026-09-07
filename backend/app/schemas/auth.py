"""Authentication request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"username": "admin", "password": "Passwd@123"}
        }
    )


class RefreshRequest(BaseModel):
    refresh_token: str


class UserSummary(ORMModel):
    id: str
    username: str
    email: str | None = None
    full_name: str | None = None
    role: str
    permissions: list[str] = Field(default_factory=list)
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None


class TokenResponse(BaseModel):
    """Issued on login and refresh.

    ``must_change_password`` is surfaced here so the UI can route straight to
    the password screen after a first sign-in with the default credential.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")
    expires_at: datetime
    must_change_password: bool = False
    user: UserSummary


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ForcedPasswordChangeRequest(BaseModel):
    """Used while ``must_change_password`` is set.

    The current password is still required - a stolen first-login token must
    not be enough to take over the account.
    """

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class PasswordPolicyInfo(BaseModel):
    min_length: int
    requires_lowercase: bool = True
    requires_uppercase: bool = True
    requires_digit: bool = True
    requires_special: bool = True
