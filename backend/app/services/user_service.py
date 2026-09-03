"""Authentication and user management."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.enums import ROLE_PERMISSIONS, RoleName
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.models.user import Permission, Role, User

logger = get_logger(__name__)


class AuthError(Exception):
    """Login failed. The message is safe to show a user."""

    def __init__(self, message: str, *, code: str = "invalid_credentials") -> None:
        super().__init__(message)
        self.code = code


class AccountLocked(AuthError):
    def __init__(self, until: datetime) -> None:
        super().__init__(
            "Account temporarily locked after repeated failed sign-in attempts.",
            code="account_locked",
        )
        self.until = until


class PasswordPolicyError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def enforce_password_policy(password: str, *, username: str | None = None) -> None:
    problems = validate_password_strength(password)
    if username and password.lower() == username.lower():
        problems.append("must not be the same as the username")
    if problems:
        raise PasswordPolicyError("Password " + "; ".join(problems) + ".")


# ------------------------------------------------------------ roles & perms
async def ensure_roles(session: AsyncSession) -> dict[str, Role]:
    """Create the role/permission rows described by ROLE_PERMISSIONS.

    Idempotent, and additive: a permission added to the enum in a later release
    is granted to its roles the next time the application boots.
    """
    all_codes: set[str] = set()
    for codes in ROLE_PERMISSIONS.values():
        all_codes.update(codes)

    existing_permissions = {
        p.code: p
        for p in (await session.execute(select(Permission))).scalars().all()
    }
    for code in sorted(all_codes):
        if code not in existing_permissions:
            permission = Permission(code=code, description=code.replace(":", " "))
            session.add(permission)
            existing_permissions[code] = permission
    await session.flush()

    roles = {
        r.name: r
        for r in (
            await session.execute(select(Role).options(selectinload(Role.permissions)))
        )
        .scalars()
        .all()
    }
    for role_name, codes in ROLE_PERMISSIONS.items():
        role = roles.get(role_name)
        wanted = [existing_permissions[code] for code in sorted(codes)]

        if role is None:
            role = Role(
                name=role_name,
                description=f"Built-in {role_name} role",
                is_system=True,
            )
            # Populate the collection BEFORE adding/flushing. Assigning it on a
            # pending object initialises it locally; reading `role.permissions`
            # after a flush would instead emit a lazy SELECT, which raises
            # MissingGreenlet under asyncio and previously broke first boot on
            # an empty database.
            role.permissions = wanted
            session.add(role)
            roles[role_name] = role
        else:
            # Loaded eagerly by the selectinload above, so this is in memory.
            current = {p.code for p in role.permissions}
            for permission in wanted:
                if permission.code not in current:
                    role.permissions.append(permission)

    await session.flush()
    return roles


async def get_role(session: AsyncSession, name: str) -> Role | None:
    return (
        await session.execute(
            select(Role).options(selectinload(Role.permissions)).where(Role.name == name)
        )
    ).scalar_one_or_none()


# ------------------------------------------------------------------- lookup
def user_query():
    return select(User).options(
        selectinload(User.role).selectinload(Role.permissions)
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (
        await session.execute(user_query().where(User.id == user_id))
    ).scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    return (
        await session.execute(
            user_query().where(func.lower(User.username) == username.strip().lower())
        )
    ).scalar_one_or_none()


async def count_active_admins(session: AsyncSession, *, exclude: uuid.UUID | None = None) -> int:
    stmt = (
        select(func.count(User.id))
        .join(Role, Role.id == User.role_id)
        .where(Role.name == RoleName.ADMIN.value, User.is_active.is_(True))
    )
    if exclude is not None:
        stmt = stmt.where(User.id != exclude)
    return int((await session.execute(stmt)).scalar() or 0)


# ----------------------------------------------------------------- sign-in
async def authenticate(
    session: AsyncSession, username: str, password: str
) -> User:
    """Verify credentials, applying lockout rules.

    The same generic error is raised for an unknown user and a wrong password
    so the response cannot be used to enumerate accounts. Lockout is reported
    distinctly because the user genuinely needs to know why waiting is
    required.
    """
    user = await get_user_by_username(session, username)

    if user is None:
        # Spend comparable time on a miss so response timing does not reveal
        # whether the username exists.
        verify_password(password, hash_password("timing-equaliser"))
        raise AuthError("Incorrect username or password.")

    locked_until = _aware(user.locked_until)
    if locked_until and locked_until > _now():
        raise AccountLocked(locked_until)

    if not verify_password(password, user.hashed_password):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_ATTEMPTS:
            user.locked_until = _now() + timedelta(
                minutes=settings.ACCOUNT_LOCKOUT_MINUTES
            )
            user.failed_login_attempts = 0
            logger.warning(
                "account_locked",
                username=user.username,
                minutes=settings.ACCOUNT_LOCKOUT_MINUTES,
            )
        await session.flush()
        raise AuthError("Incorrect username or password.")

    if not user.is_active:
        raise AuthError(
            "This account is disabled. Contact an administrator.",
            code="account_disabled",
        )

    # Successful login: clear the counters and opportunistically upgrade the
    # hash if the bcrypt cost has since been raised.
    user.failed_login_attempts = 0
    user.locked_until = None
    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)
    await session.flush()
    return user


async def note_login(
    session: AsyncSession, user: User, *, ip_address: str | None
) -> None:
    user.last_login_at = _now()
    user.last_login_ip = ip_address
    await session.flush()


# ---------------------------------------------------------------- mutations
async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    role_name: str,
    email: str | None = None,
    full_name: str | None = None,
    is_active: bool = True,
    must_change_password: bool = True,
    created_by_id: uuid.UUID | None = None,
) -> User:
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if len(username) < 3 or len(username) > 64:
        raise ValueError("username must be between 3 and 64 characters")
    if not all(ch.isalnum() or ch in "._-" for ch in username):
        raise ValueError(
            "username may contain only letters, digits, dots, underscores and hyphens"
        )

    enforce_password_policy(password, username=username)

    role = await get_role(session, role_name)
    if role is None:
        raise ValueError(f"unknown role '{role_name}'")

    clauses = [func.lower(User.username) == username.lower()]
    normalised_email = (email or "").strip().lower()
    if normalised_email:
        clauses.append(func.lower(User.email) == normalised_email)
    existing = (
        await session.execute(select(User.id).where(or_(*clauses)).limit(1))
    ).first()
    if existing is not None:
        raise ValueError("a user with that username or e-mail already exists")

    user = User(
        username=username,
        email=(email or "").strip() or None,
        full_name=(full_name or "").strip() or None,
        hashed_password=hash_password(password),
        role_id=role.id,
        is_active=is_active,
        must_change_password=must_change_password,
        password_changed_at=_now(),
        created_by_id=created_by_id,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ValueError("a user with that username or e-mail already exists") from exc
    user.role = role
    return user


async def change_password(
    session: AsyncSession,
    user: User,
    *,
    current_password: str | None,
    new_password: str,
    require_current: bool = True,
) -> None:
    if require_current:
        if not current_password or not verify_password(
            current_password, user.hashed_password
        ):
            raise AuthError("Current password is incorrect.", code="invalid_password")
        if current_password == new_password:
            raise PasswordPolicyError(
                "New password must be different from the current password."
            )

    enforce_password_policy(new_password, username=user.username)

    user.hashed_password = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = _now()
    user.failed_login_attempts = 0
    user.locked_until = None
    # Invalidate every token issued before this change.
    user.token_version = (user.token_version or 0) + 1
    await session.flush()
    logger.info("password_changed", username=user.username)


async def reset_password(
    session: AsyncSession,
    user: User,
    *,
    new_password: str,
    force_change: bool = True,
) -> None:
    """Administrative reset - no current password required."""
    enforce_password_policy(new_password, username=user.username)
    user.hashed_password = hash_password(new_password)
    user.must_change_password = force_change
    user.password_changed_at = _now()
    user.failed_login_attempts = 0
    user.locked_until = None
    user.token_version = (user.token_version or 0) + 1
    await session.flush()
    logger.info("password_reset", username=user.username)


async def update_user(
    session: AsyncSession,
    user: User,
    payload: dict[str, Any],
    *,
    acting_user: User,
) -> dict[str, Any]:
    """Apply a partial user update, guarding the last-admin invariant."""
    changes: dict[str, Any] = {}

    if "email" in payload:
        new_email = (payload["email"] or "").strip() or None
        if new_email != user.email:
            if new_email:
                clash = (
                    await session.execute(
                        select(User.id).where(
                            func.lower(User.email) == new_email.lower(),
                            User.id != user.id,
                        )
                    )
                ).first()
                if clash is not None:
                    raise ValueError("that e-mail address is already in use")
            changes["email"] = {"from": user.email, "to": new_email}
            user.email = new_email

    if "full_name" in payload:
        new_name = (payload["full_name"] or "").strip() or None
        if new_name != user.full_name:
            changes["full_name"] = {"from": user.full_name, "to": new_name}
            user.full_name = new_name

    if "role" in payload and payload["role"]:
        role = await get_role(session, str(payload["role"]))
        if role is None:
            raise ValueError(f"unknown role '{payload['role']}'")
        if role.id != user.role_id:
            demoting_admin = (
                user.role_name == RoleName.ADMIN.value
                and role.name != RoleName.ADMIN.value
            )
            if demoting_admin and await count_active_admins(
                session, exclude=user.id
            ) == 0:
                raise ValueError(
                    "cannot change the role of the last active administrator"
                )
            if user.id == acting_user.id and demoting_admin:
                raise ValueError("you cannot remove your own administrator role")
            changes["role"] = {"from": user.role_name, "to": role.name}
            user.role_id = role.id
            user.role = role
            # Role changes must take effect immediately, not at token expiry.
            user.token_version = (user.token_version or 0) + 1

    if "is_active" in payload and payload["is_active"] is not None:
        is_active = bool(payload["is_active"])
        if is_active != user.is_active:
            if not is_active:
                if user.id == acting_user.id:
                    raise ValueError("you cannot disable your own account")
                if (
                    user.role_name == RoleName.ADMIN.value
                    and await count_active_admins(session, exclude=user.id) == 0
                ):
                    raise ValueError(
                        "cannot disable the last active administrator"
                    )
            changes["is_active"] = {"from": user.is_active, "to": is_active}
            user.is_active = is_active
            if not is_active:
                user.token_version = (user.token_version or 0) + 1

    if "must_change_password" in payload and payload["must_change_password"] is not None:
        value = bool(payload["must_change_password"])
        if value != user.must_change_password:
            changes["must_change_password"] = {
                "from": user.must_change_password,
                "to": value,
            }
            user.must_change_password = value

    if "unlock" in payload and payload["unlock"]:
        if user.locked_until is not None or user.failed_login_attempts:
            changes["unlocked"] = {"from": True, "to": False}
        user.locked_until = None
        user.failed_login_attempts = 0

    await session.flush()
    return changes


async def delete_user(
    session: AsyncSession, user: User, *, acting_user: User
) -> None:
    if user.id == acting_user.id:
        raise ValueError("you cannot delete your own account")
    if (
        user.role_name == RoleName.ADMIN.value
        and await count_active_admins(session, exclude=user.id) == 0
    ):
        raise ValueError("cannot delete the last active administrator")
    await session.delete(user)
    await session.flush()
    logger.info("user_deleted", username=user.username, by=acting_user.username)


# --------------------------------------------------------------- bootstrap
async def ensure_default_admin(session: AsyncSession) -> tuple[User, bool]:
    """Create the initial administrator described by the environment.

    The password is read from ADMIN_PASSWORD and stored only as a bcrypt
    digest. If the account already exists, nothing is overwritten - a
    redeploy must never reset a password an operator has changed.
    """
    roles = await ensure_roles(session)
    admin_role = roles[RoleName.ADMIN.value]

    username = settings.ADMIN_USERNAME.strip()
    existing = await get_user_by_username(session, username)
    if existing is not None:
        return existing, False

    user = User(
        username=username,
        email=settings.ADMIN_EMAIL or None,
        full_name="Administrator",
        hashed_password=hash_password(settings.ADMIN_PASSWORD),
        role_id=admin_role.id,
        is_active=True,
        # The default credential is public knowledge, so the first sign-in has
        # to end in a password change.
        must_change_password=settings.ADMIN_FORCE_PASSWORD_CHANGE,
        password_changed_at=_now(),
    )
    session.add(user)
    await session.flush()
    user.role = admin_role
    logger.warning(
        "default_admin_created",
        username=username,
        must_change_password=user.must_change_password,
        detail="sign in and change this password immediately",
    )
    return user, True
