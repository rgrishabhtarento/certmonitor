"""Public branding.

Unauthenticated on purpose: the sign-in screen has to render the deployment's
name and logo before anyone has a session, exactly as ``/health`` has to
answer a probe that cannot present a token.

Only the two branding settings are exposed. Nothing else from
``system_settings`` may be added here - the rest of that table is
admin-only and reachable through ``/api/v1/settings``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.schemas.system import BrandingRead
from app.services import settings_service

logger = get_logger(__name__)

router = APIRouter(tags=["Branding"])

DEFAULT_APP_NAME = "InfraSight"


@router.get(
    "/branding",
    response_model=BrandingRead,
    summary="Deployment name and logo",
)
async def branding(session: DbSession) -> BrandingRead:
    """Name and logo text for the current deployment.

    Falls back to the built-in name if the setting is blank or the table is
    unreadable - a branding lookup must never be what stops the sign-in page
    from rendering.
    """
    try:
        config = await settings_service.load_settings(session)
    except Exception:  # pragma: no cover - defensive
        logger.warning("branding_unavailable", exc_info=True)
        return BrandingRead(app_name=DEFAULT_APP_NAME, logo_text="")

    name = str(config.get("branding_app_name") or "").strip()
    return BrandingRead(
        app_name=name or DEFAULT_APP_NAME,
        logo_text=str(config.get("branding_logo_text") or "").strip(),
    )
