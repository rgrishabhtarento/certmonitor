"""FastAPI dependencies: authentication, authorisation and common params."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Callable

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import Permission, RoleName
from app.core.logging import get_logger
from app.core.security import decode_token
from app.models.user import User
from app.schemas.common import PaginationParams
from app.services import settings_service, user_service

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    request: Request,
    session: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    """Resolve the caller from a bearer token.

    The token's ``tv`` claim is compared against the user's ``token_version``,
    so a password change, a role change or a disable takes effect immediately
    rather than at token expiry.
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise _UNAUTHENTICATED from exc

    user = await user_service.get_user(session, user_id)
    if user is None:
        raise _UNAUTHENTICATED
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is disabled.",
        )
    if int(payload.get("tv", 0)) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer valid. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_active_user(user: CurrentUser) -> User:
    """Reject a user who still has to change their password.

    Only the password-change and profile routes accept such a session; every
    other route is closed until the default credential has been replaced.
    """
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before continuing.",
            headers={"X-Password-Change-Required": "true"},
        )
    return user


ActiveUser = Annotated[User, Depends(get_active_user)]


def require_permissions(*codes: str) -> Callable[..., Any]:
    """Dependency factory enforcing that the caller holds every code given."""

    async def dependency(user: ActiveUser) -> User:
        missing = [code for code in codes if not user.has_permission(code)]
        if missing:
            logger.info(
                "authorisation_denied",
                username=user.username,
                role=user.role_name,
                missing=missing,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your role does not permit this action."
                    if len(missing) == 1
                    else "Your role does not permit this action."
                ),
            )
        return user

    return dependency


def require_admin() -> Callable[..., Any]:
    async def dependency(user: ActiveUser) -> User:
        if user.role_name != RoleName.ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access is required.",
            )
        return user

    return dependency


# Frequently used role/permission gates, named for readability at call sites.
ReadEndpoints = Annotated[User, Depends(require_permissions(Permission.ENDPOINT_READ))]
WriteEndpoints = Annotated[User, Depends(require_permissions(Permission.ENDPOINT_WRITE))]
DeleteEndpoints = Annotated[
    User, Depends(require_permissions(Permission.ENDPOINT_DELETE))
]
CheckEndpoints = Annotated[User, Depends(require_permissions(Permission.ENDPOINT_CHECK))]
ImportEndpoints = Annotated[
    User, Depends(require_permissions(Permission.ENDPOINT_IMPORT))
]
ExportEndpoints = Annotated[
    User, Depends(require_permissions(Permission.ENDPOINT_EXPORT))
]
ManageUsers = Annotated[User, Depends(require_permissions(Permission.USER_WRITE))]
ReadUsers = Annotated[User, Depends(require_permissions(Permission.USER_READ))]
ReadSettings = Annotated[User, Depends(require_permissions(Permission.SETTINGS_READ))]
WriteSettings = Annotated[User, Depends(require_permissions(Permission.SETTINGS_WRITE))]
ReadAlerts = Annotated[User, Depends(require_permissions(Permission.ALERT_READ))]
WriteAlerts = Annotated[User, Depends(require_permissions(Permission.ALERT_WRITE))]
ReadIncidents = Annotated[User, Depends(require_permissions(Permission.INCIDENT_READ))]
WriteIncidents = Annotated[User, Depends(require_permissions(Permission.INCIDENT_WRITE))]
ReadAudit = Annotated[User, Depends(require_permissions(Permission.AUDIT_READ))]
WriteTags = Annotated[User, Depends(require_permissions(Permission.TAG_WRITE))]
WriteEnvironments = Annotated[
    User, Depends(require_permissions(Permission.ENVIRONMENT_WRITE))
]
WriteNotifications = Annotated[
    User, Depends(require_permissions(Permission.NOTIFICATION_WRITE))
]
AdminUser = Annotated[User, Depends(require_admin())]


# ------------------------------------------------------------- parameters
async def pagination(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=200, description="Rows per page (max 200).")
    ] = 25,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


Pagination = Annotated[PaginationParams, Depends(pagination)]


async def runtime_config(session: DbSession) -> dict[str, Any]:
    """Effective settings for this request (cached briefly in-process)."""
    return await settings_service.load_settings(session)


RuntimeConfig = Annotated[dict[str, Any], Depends(runtime_config)]


def parse_uuid_list(values: list[str] | None) -> list[uuid.UUID]:
    """Turn repeated query params into UUIDs, rejecting malformed entries."""
    parsed: list[uuid.UUID] = []
    for raw in values or []:
        for chunk in str(raw).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed.append(uuid.UUID(chunk))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"'{chunk}' is not a valid identifier.",
                ) from exc
    return parsed


def split_csv_param(values: list[str] | None) -> list[str]:
    """Accept both ``?status=up&status=down`` and ``?status=up,down``."""
    parsed: list[str] = []
    for raw in values or []:
        for chunk in str(raw).split(","):
            chunk = chunk.strip()
            if chunk:
                parsed.append(chunk)
    return parsed
