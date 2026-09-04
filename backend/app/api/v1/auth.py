"""Authentication routes."""

from __future__ import annotations

from datetime import timedelta

import jwt
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import settings
from app.core.enums import AuditAction
from app.core.logging import get_logger
from app.core.ratelimit import check_login_rate_limit, clear_login_rate_limit
from app.core.security import create_token, decode_token
from app.api.deps import CurrentUser, DbSession
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    PasswordPolicyInfo,
    RefreshRequest,
    TokenResponse,
    UserSummary,
)
from app.schemas.common import Message
from app.services import audit_service, settings_service, user_service
from app.services.audit_service import client_ip
from app.services.user_service import (
    AccountLocked,
    AuthError,
    PasswordPolicyError,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _user_summary(user) -> UserSummary:
    return UserSummary(
        id=str(user.id),
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role_name,
        permissions=sorted(user.permissions),
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
    )


def _issue_tokens(user, config: dict | None = None) -> TokenResponse:
    """Mint an access/refresh pair.

    Lifetimes come from the runtime settings when the caller has them, so an
    administrator can change the session timeout from the Settings page
    without a redeploy. The environment values remain the fallback and the
    seeded default.

    Only sessions created from here on pick up a new value - a token already
    in a browser carries its own expiry, and shortening the setting cannot
    reach back and revoke it. Bumping the user's `token_version` is what does
    that, and that is what a password reset or a role change already does.
    """
    config = config or {}
    access_minutes = int(
        config.get("session_timeout_minutes")
        or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    refresh_days = int(
        config.get("session_refresh_days") or settings.REFRESH_TOKEN_EXPIRE_DAYS
    )

    access_token, expires_at = create_token(
        str(user.id),
        "access",
        role=user.role_name,
        token_version=user.token_version or 0,
        expires_delta=timedelta(minutes=access_minutes),
    )
    refresh_token, _ = create_token(
        str(user.id),
        "refresh",
        role=user.role_name,
        token_version=user.token_version or 0,
        expires_delta=timedelta(days=refresh_days),
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=access_minutes * 60,
        expires_at=expires_at,
        must_change_password=user.must_change_password,
        user=_user_summary(user),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in and obtain an access token",
    responses={
        401: {"model": Message, "description": "Invalid credentials"},
        423: {"model": Message, "description": "Account temporarily locked"},
        429: {"model": Message, "description": "Too many attempts"},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
) -> TokenResponse:
    """Exchange a username and password for a JWT pair.

    Attempts are rate limited per source address and per username, and the
    account itself locks after a configurable number of failures. Both the
    username and password being wrong produce the same response so accounts
    cannot be enumerated.
    """
    ip = client_ip(request) or "unknown"
    username = payload.username.strip()

    for identifier in (f"ip:{ip}", f"user:{username.lower()}"):
        result = await check_login_rate_limit(identifier)
        if not result.allowed:
            response.headers["Retry-After"] = str(result.retry_after_seconds)
            await audit_service.record(
                session,
                action=AuditAction.LOGIN_FAILED.value,
                username=username,
                status="rate_limited",
                details={"reason": "rate_limit_exceeded"},
                request=request,
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many sign-in attempts. Try again in "
                    f"{result.retry_after_seconds} seconds."
                ),
                headers={"Retry-After": str(result.retry_after_seconds)},
            )

    try:
        user = await user_service.authenticate(
            session,
            username,
            payload.password,
            await settings_service.load_settings(session),
        )
    except AccountLocked as exc:
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED.value,
            username=username,
            status="locked",
            details={"locked_until": exc.until.isoformat()},
            request=request,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED, detail=str(exc)
        ) from exc
    except AuthError as exc:
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED.value,
            username=username,
            status="failure",
            details={"code": exc.code},
            request=request,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    await user_service.note_login(session, user, ip_address=ip)
    await clear_login_rate_limit(f"user:{username.lower()}")
    await clear_login_rate_limit(f"ip:{ip}")

    await audit_service.record(
        session,
        action=AuditAction.LOGIN.value,
        user=user,
        details={"role": user.role_name},
        request=request,
    )
    await session.commit()

    tokens = _issue_tokens(user, await settings_service.load_settings(session))
    logger.info(
        "login_succeeded",
        username=user.username,
        role=user.role_name,
        must_change_password=user.must_change_password,
    )
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new access token",
)
async def refresh(payload: RefreshRequest, session: DbSession) -> TokenResponse:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        ) from exc

    import uuid as _uuid

    try:
        user_id = _uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
        ) from exc

    user = await user_service.get_user(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
        )
    if int(claims.get("tv", 0)) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer valid. Please sign in again.",
        )
    return _issue_tokens(user, await settings_service.load_settings(session))


@router.post("/logout", response_model=Message, summary="Sign out")
async def logout(
    user: CurrentUser, request: Request, session: DbSession
) -> Message:
    """Record the sign-out.

    Access tokens are stateless and short-lived, so the client discards them;
    an administrator can force immediate invalidation everywhere by resetting
    the user's password, which bumps ``token_version``.
    """
    await audit_service.record(
        session, action=AuditAction.LOGOUT.value, user=user, request=request
    )
    await session.commit()
    return Message(detail="Signed out.")


@router.get(
    "/me", response_model=UserSummary, summary="Details of the signed-in user"
)
async def me(user: CurrentUser) -> UserSummary:
    return _user_summary(user)


@router.get(
    "/password-policy",
    response_model=PasswordPolicyInfo,
    summary="Password requirements, for client-side validation",
)
async def password_policy() -> PasswordPolicyInfo:
    return PasswordPolicyInfo(min_length=settings.PASSWORD_MIN_LENGTH)


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="Change your own password",
)
async def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    request: Request,
    session: DbSession,
) -> TokenResponse:
    """Replace the caller's password.

    This route is intentionally reachable while ``must_change_password`` is
    set - it is the only way out of that state. A fresh token pair is returned
    because the change invalidates the caller's current tokens.
    """
    try:
        await user_service.change_password(
            session,
            user,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthError as exc:
        await audit_service.record(
            session,
            action=AuditAction.PASSWORD_CHANGED.value,
            user=user,
            status="failure",
            details={"code": exc.code},
            request=request,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.PASSWORD_CHANGED.value,
        user=user,
        resource_type="user",
        resource_id=user.id,
        resource_name=user.username,
        request=request,
    )
    await session.commit()
    return _issue_tokens(user, await settings_service.load_settings(session))
