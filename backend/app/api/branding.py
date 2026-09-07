"""Public branding: the deployment's name and uploaded logo.

Unauthenticated on purpose: the sign-in screen has to render the name and
logo before anyone has a session, exactly as ``/health`` has to answer a probe
that cannot present a token.

Only branding is exposed. Nothing else from ``system_settings`` may be added
here - the rest of that table is admin-only and reachable through
``/api/v1/settings``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.schemas.system import BrandingRead
from app.services import branding_service, settings_service

logger = get_logger(__name__)

router = APIRouter(tags=["Branding"])

DEFAULT_APP_NAME = "InfraSight"

# A year, because the URL carries the content hash: a new upload is a new URL,
# so the cached copy of an old one can never be wrong.
_LOGO_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get(
    "/branding",
    response_model=BrandingRead,
    summary="Deployment name and logo",
)
async def branding(session: DbSession) -> BrandingRead:
    """Name and logo for the current deployment.

    Falls back to the built-in name if the settings or asset lookup fails - a
    branding lookup must never be what stops the sign-in page from rendering.
    """
    try:
        config = await settings_service.load_settings(session)
        asset = await branding_service.get_logo(session)
    except Exception:  # pragma: no cover - defensive
        logger.warning("branding_unavailable", exc_info=True)
        return BrandingRead(app_name=DEFAULT_APP_NAME, logo_url=None)

    name = str(config.get("branding_app_name") or "").strip()
    return BrandingRead(
        app_name=name or DEFAULT_APP_NAME,
        logo_url=branding_service.logo_url(asset),
    )


@router.get(
    "/branding/logo",
    summary="The uploaded logo image",
    responses={
        200: {"content": {"image/png": {}}, "description": "The logo"},
        404: {"description": "No logo has been uploaded"},
    },
)
async def branding_logo(request: Request, session: DbSession) -> Response:
    """Serve the logo bytes.

    Headers are chosen for a file that arrives from an upload and is served
    from the application's own origin: ``nosniff`` so the browser cannot be
    talked into treating it as anything but the sniffed image type, and a
    locked-down CSP so it stays inert even if opened directly.
    """
    asset = await branding_service.get_logo(session)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No logo has been uploaded."
        )

    quoted = f'"{asset.etag}"'
    if request.headers.get("if-none-match") == quoted:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": quoted})

    return Response(
        content=asset.data,
        media_type=asset.content_type,
        headers={
            "ETag": quoted,
            "Cache-Control": _LOGO_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "Content-Disposition": "inline",
        },
    )
