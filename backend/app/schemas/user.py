"""User management schemas."""

from __future__ import annotations

import unicodedata
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.enums import RoleName
from app.schemas.common import ORMModel

# An avatar is one emoji, so it is validated by Unicode category rather than
# by matching a list that would go stale with every Unicode release:
#
#   So  the pictograph itself, and each half of a regional-indicator flag
#   Sk  skin-tone modifiers
#   Mn  variation selectors (U+FE0F and friends)
#   Cf  the zero-width joiner that welds 👨 + 💻 into 👨‍💻
#
# Requiring at least one So rejects plain text, markup and lone modifiers,
# while the codepoint cap keeps a ZWJ chain from filling the column. Keycap
# sequences (1️⃣) are the known exclusion - they start with an ASCII digit.
_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Cf"}
MAX_AVATAR_CODEPOINTS = 8
_ZWJ = "‍"
# Flags are a pair of regional-indicator letters, the one legitimate case of
# two unjoined pictographs in a single grapheme.
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F1FF + 1)


def validate_avatar_emoji(value: str | None) -> str | None:
    """Normalise an avatar to one emoji, or to None when cleared.

    Blank input clears the avatar rather than erroring: the UI's "use my
    initials" choice submits an empty string.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_AVATAR_CODEPOINTS:
        raise ValueError("avatar_emoji must be a single emoji")

    categories = [unicodedata.category(char) for char in cleaned]
    if any(category not in _EMOJI_CATEGORIES for category in categories):
        raise ValueError("avatar_emoji must be an emoji, not text")

    # Count pictographs that start a grapheme. One is the normal case; a ZWJ
    # sequence such as 👨‍💻 has one base plus joined parts; a flag has two
    # regional indicators. Anything else is several emoji in one field, which
    # would overflow the round avatar it is drawn in.
    bases = [
        index
        for index, char in enumerate(cleaned)
        if categories[index] == "So" and not (index and cleaned[index - 1] == _ZWJ)
    ]
    if not bases:
        raise ValueError("avatar_emoji must be an emoji")
    if len(bases) > 1:
        is_flag = len(cleaned) == 2 and all(
            ord(char) in _REGIONAL_INDICATORS for char in cleaned
        )
        if not is_flag:
            raise ValueError("avatar_emoji must be a single emoji")
    return cleaned


class UserRead(ORMModel):
    id: str
    username: str
    email: str | None = None
    full_name: str | None = None
    # Free-text team label, used for RCA ownership.
    team: str | None = None
    avatar_emoji: str | None = None
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


class ProfileUpdate(BaseModel):
    """What a user may change about their own account.

    Deliberately narrow. Role, activation and lockout stay on ``UserUpdate``
    behind the user-management permission, so this cannot be used to grant
    yourself anything.
    """

    full_name: str | None = Field(default=None, max_length=128)
    avatar_emoji: str | None = Field(
        default=None,
        max_length=32,
        description="A single emoji. An empty string clears it.",
    )

    @field_validator("avatar_emoji")
    @classmethod
    def _one_emoji(cls, value: str | None) -> str | None:
        return validate_avatar_emoji(value)


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


class SignInResetResult(BaseModel):
    """What was actually cleared, so the confirmation is specific.

    "Unlocked" is too vague when two different throttles could have been the
    thing blocking the person - the counts say which one it was.
    """

    detail: str
    was_locked: bool = False
    failed_attempts_cleared: int = 0
    addresses_cleared: int = Field(
        default=0,
        description="Source addresses whose login rate limit was also lifted.",
    )
    user: "UserRead"
