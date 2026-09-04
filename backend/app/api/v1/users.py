"""User and role administration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, ManageUsers, ReadUsers
from app.core.config import settings
from app.core.enums import AuditAction
from app.core.ratelimit import clear_login_rate_limit
from app.core.logging import get_logger
from app.models.system import AuditLog
from app.models.user import Role, User
from app.schemas.common import Message, Page
from app.schemas.user import (
    PasswordResetRequest,
    RoleRead,
    SignInResetResult,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import audit_service, user_service
from app.services.user_service import PasswordPolicyError

logger = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["Users"])


def _to_schema(user: User) -> UserRead:
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return UserRead(
        id=str(user.id),
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role_name,
        permissions=sorted(user.permissions),
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        is_locked=bool(locked_until and locked_until > datetime.now(timezone.utc)),
        locked_until=locked_until,
        failed_login_attempts=user.failed_login_attempts or 0,
        last_login_at=user.last_login_at,
        last_login_ip=user.last_login_ip,
        password_changed_at=user.password_changed_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def _load_user(session, user_id: uuid.UUID) -> User:
    user = await user_service.get_user(session, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found."
        )
    return user


@router.get("", response_model=Page[UserRead], summary="List users")
async def list_users(
    session: DbSession,
    _user: ReadUsers,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Annotated[str | None, Query()] = None,
    role: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[UserRead]:
    stmt = select(User).options(
        selectinload(User.role).selectinload(Role.permissions)
    )
    if search:
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(needle),
                func.lower(func.coalesce(User.email, "")).like(needle),
                func.lower(func.coalesce(User.full_name, "")).like(needle),
            )
        )
    if role:
        stmt = stmt.join(Role, Role.id == User.role_id).where(
            Role.name == role.strip().lower()
        )
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    total = int(
        (
            await session.execute(
                stmt.with_only_columns(func.count(User.id)).order_by(None)
            )
        ).scalar()
        or 0
    )
    rows = list(
        (
            await session.execute(
                stmt.order_by(User.username)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return Page.build(
        [_to_schema(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/roles", response_model=list[RoleRead], summary="Roles and their permissions"
)
async def list_roles(session: DbSession, _user: ReadUsers) -> list[RoleRead]:
    rows = (
        await session.execute(
            select(Role, func.count(User.id))
            .options(selectinload(Role.permissions))
            .outerjoin(User, User.role_id == Role.id)
            .group_by(Role.id)
            .order_by(Role.name)
        )
    ).unique().all()
    return [
        RoleRead(
            id=str(role.id),
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=sorted(p.code for p in role.permissions),
            user_count=int(count or 0),
        )
        for role, count in rows
    ]


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
)
async def create_user(
    payload: UserCreate,
    admin: ManageUsers,
    request: Request,
    session: DbSession,
) -> UserRead:
    """Create an account.

    The password supplied here is a temporary one: unless the caller opts out,
    the new user must replace it at first sign-in.
    """
    try:
        user = await user_service.create_user(
            session,
            username=payload.username,
            password=payload.password,
            role_name=payload.role,
            email=str(payload.email) if payload.email else None,
            full_name=payload.full_name,
            team=payload.team,
            is_active=payload.is_active,
            must_change_password=payload.must_change_password,
            created_by_id=admin.id,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.USER_CREATED.value,
        user=admin,
        resource_type="user",
        resource_id=user.id,
        resource_name=user.username,
        details={
            "role": user.role_name,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
        },
        request=request,
    )
    await session.commit()
    return _to_schema(user)


@router.get("/{user_id}", response_model=UserRead, summary="User details")
async def get_user(
    user_id: uuid.UUID, session: DbSession, _user: ReadUsers
) -> UserRead:
    return _to_schema(await _load_user(session, user_id))


@router.put("/{user_id}", response_model=UserRead, summary="Update a user")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    admin: ManageUsers,
    request: Request,
    session: DbSession,
) -> UserRead:
    """Change a user's details, role or status.

    Guards the last-admin invariant: the final active administrator cannot be
    demoted, disabled or deleted, so an instance can never be locked out of
    its own administration.
    """
    target = await _load_user(session, user_id)
    try:
        changes = await user_service.update_user(
            session,
            target,
            payload.model_dump(exclude_unset=True),
            acting_user=admin,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if changes:
        action = AuditAction.USER_UPDATED.value
        if "role" in changes:
            action = AuditAction.ROLE_CHANGED.value
        elif "is_active" in changes:
            action = (
                AuditAction.USER_ENABLED.value
                if changes["is_active"]["to"]
                else AuditAction.USER_DISABLED.value
            )
        await audit_service.record(
            session,
            action=action,
            user=admin,
            resource_type="user",
            resource_id=target.id,
            resource_name=target.username,
            details={"changes": changes},
            request=request,
        )
    await session.commit()
    return _to_schema(target)


@router.post(
    "/{user_id}/reset-password",
    response_model=Message,
    summary="Reset a user's password",
)
async def reset_password(
    user_id: uuid.UUID,
    payload: PasswordResetRequest,
    admin: ManageUsers,
    request: Request,
    session: DbSession,
) -> Message:
    """Set a new password for another user.

    Every session that user currently holds is invalidated, because the reset
    bumps their token version.
    """
    target = await _load_user(session, user_id)
    try:
        await user_service.reset_password(
            session,
            target,
            new_password=payload.new_password,
            force_change=payload.force_change,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.PASSWORD_RESET.value,
        user=admin,
        resource_type="user",
        resource_id=target.id,
        resource_name=target.username,
        details={"force_change": payload.force_change},
        request=request,
    )
    await session.commit()
    return Message(
        detail=(
            f"Password reset for '{target.username}'. "
            "Their existing sessions have been invalidated."
        )
    )


@router.delete("/{user_id}", response_model=Message, summary="Delete a user")
async def delete_user(
    user_id: uuid.UUID,
    admin: ManageUsers,
    request: Request,
    session: DbSession,
) -> Message:
    target = await _load_user(session, user_id)
    username = target.username
    try:
        await user_service.delete_user(session, target, acting_user=admin)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.USER_DELETED.value,
        user=admin,
        resource_type="user",
        resource_id=user_id,
        resource_name=username,
        request=request,
    )
    await session.commit()
    return Message(detail=f"User '{username}' deleted.")


@router.post(
    "/{user_id}/reset-lockout",
    response_model=SignInResetResult,
    summary="Clear a user's lockout and login rate limit",
)
async def reset_sign_in_limits(
    user_id: uuid.UUID,
    admin: ManageUsers,
    request: Request,
    session: DbSession,
) -> SignInResetResult:
    """Let someone sign in again, now.

    There are **two** independent throttles on the sign-in path and they trip
    at different points, which is why clearing one was never enough:

    * the **account lockout** - `failed_login_attempts` reaching the
      configured limit, which sets `locked_until` on the user row, and
    * the **login rate limiter** - a shorter, sharper window keyed separately
      on the username *and* on the source address, held in Redis rather than
      on the user.

    Someone who fumbles their password a few times usually trips the rate
    limiter first and gets a 429 telling them to wait. Nothing in the product
    could clear that: the existing "unlock" only reset the row, so an
    administrator standing next to the person could still do nothing but tell
    them to wait five minutes.

    This clears both. The address side is recovered from the recent
    failed-login audit entries for that username, because the limiter is keyed
    on IP as well and lifting only the username key would leave them blocked
    by the other one.
    """
    target = await _load_user(session, user_id)
    was_locked = bool(target.locked_until)
    attempts = target.failed_login_attempts or 0

    # ---- the row
    target.locked_until = None
    target.failed_login_attempts = 0

    # ---- the username key
    await clear_login_rate_limit(f"user:{target.username.lower()}")

    # ---- and every address that recently failed against this username
    since = datetime.now(timezone.utc) - timedelta(
        seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS * 2
    )
    addresses = [
        row
        for row in (
            await session.execute(
                select(AuditLog.ip_address)
                .where(
                    AuditLog.action == AuditAction.LOGIN_FAILED.value,
                    func.lower(AuditLog.username) == target.username.lower(),
                    AuditLog.created_at >= since,
                    AuditLog.ip_address.is_not(None),
                )
                .distinct()
                .limit(10)
            )
        ).scalars()
        if row
    ]
    for address in addresses:
        await clear_login_rate_limit(f"ip:{address}")

    await audit_service.record(
        session,
        action=AuditAction.USER_UPDATED.value,
        user=admin,
        resource_type="user",
        resource_id=target.id,
        resource_name=target.username,
        details={
            "sign_in_limits_reset": True,
            "was_locked": was_locked,
            "failed_attempts_cleared": attempts,
            "addresses_cleared": len(addresses),
        },
        request=request,
    )
    await session.commit()

    logger.info(
        "sign_in_limits_reset",
        username=target.username,
        by=admin.username,
        was_locked=was_locked,
        failed_attempts_cleared=attempts,
        addresses_cleared=len(addresses),
    )
    return SignInResetResult(
        detail=f"'{target.username}' can sign in again immediately.",
        was_locked=was_locked,
        failed_attempts_cleared=attempts,
        addresses_cleared=len(addresses),
        user=_to_schema(target),
    )
