"""The uploaded company logo.

Stored in the database, served by an unauthenticated route, and therefore
validated strictly: the bytes reach every visitor's browser before anyone has
signed in.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.system import BrandingAsset

logger = get_logger(__name__)

LOGO_KEY = "logo"

# A logo is a header-sized image. The cap is far below MAX_UPLOAD_BYTES
# because these bytes are read from the database on cache-miss requests to a
# public route, and because nothing legitimate needs more.
MAX_LOGO_BYTES = 512 * 1024

# Raster formats only, identified by magic bytes rather than by the declared
# content type or the filename - both are attacker-controlled.
#
# SVG is deliberately absent. It is XML that can carry script, and this file
# is served from the application's own origin, so a visitor who opened the
# logo URL directly would execute it. Accepting SVG would turn "upload a
# logo" into stored XSS for anyone holding settings:write.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

ALLOWED_DESCRIPTION = "PNG, JPEG, GIF or WebP"


def sniff_content_type(data: bytes) -> str | None:
    """Identify an image from its leading bytes, or None if unrecognised."""
    for signature, content_type in _MAGIC:
        if data.startswith(signature):
            return content_type
    # WebP is a RIFF container: "RIFF" .... "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def get_logo(session: AsyncSession) -> BrandingAsset | None:
    return (
        await session.execute(
            select(BrandingAsset).where(BrandingAsset.key == LOGO_KEY)
        )
    ).scalar_one_or_none()


async def save_logo(
    session: AsyncSession,
    *,
    data: bytes,
    filename: str | None,
    user_id: object | None = None,
) -> BrandingAsset:
    """Replace the logo. Raises ``ValueError`` on anything unacceptable."""
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError(
            f"the logo must be smaller than {MAX_LOGO_BYTES // 1024} KB"
        )

    content_type = sniff_content_type(data)
    if content_type is None:
        raise ValueError(f"unsupported image format - upload {ALLOWED_DESCRIPTION}")

    etag = hashlib.sha256(data).hexdigest()[:32]
    asset = await get_logo(session)
    if asset is None:
        asset = BrandingAsset(key=LOGO_KEY)
        session.add(asset)

    asset.content_type = content_type
    asset.data = data
    asset.byte_size = len(data)
    asset.etag = etag
    asset.original_filename = (filename or None) and filename[:255]
    asset.updated_by_id = user_id
    await session.flush()
    logger.info("branding_logo_saved", content_type=content_type, bytes=len(data))
    return asset


async def delete_logo(session: AsyncSession) -> bool:
    """Remove the logo. Returns whether there was one to remove."""
    asset = await get_logo(session)
    if asset is None:
        return False
    await session.delete(asset)
    await session.flush()
    logger.info("branding_logo_deleted")
    return True


def logo_url(asset: BrandingAsset | None) -> str | None:
    """Public URL for a logo, carrying its etag so a new upload busts caches."""
    if asset is None:
        return None
    return f"/branding/logo?v={asset.etag}"
