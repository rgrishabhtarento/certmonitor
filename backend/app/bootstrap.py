"""First-boot seeding.

Runs on API startup and is safe to run repeatedly and concurrently: an
advisory lock serialises replicas so two containers starting at once cannot
race to create the admin account.

Seeding is not a substitute for migrations. The schema comes from Alembic; this
only populates rows the application cannot function without.
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.endpoint import Environment
from app.services import settings_service, user_service

logger = get_logger(__name__)

# Arbitrary but stable key for the PostgreSQL advisory lock.
_BOOTSTRAP_LOCK_KEY = 728_113_501

DEFAULT_ENVIRONMENTS = (
    ("development", "Development", "#3b82f6", 10),
    ("testing", "Testing", "#8b5cf6", 20),
    ("staging", "Staging", "#f59e0b", 30),
    ("production", "Production", "#ef4444", 40),
    ("other", "Other", "#6b7280", 90),
)


async def _acquire_lock(session: AsyncSession) -> bool:
    """Take the bootstrap advisory lock, if the backend supports one."""
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect != "postgresql":
        return True
    result = await session.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY}
    )
    return bool(result.scalar())


async def _release_lock(session: AsyncSession) -> None:
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY}
    )


async def seed_environments(session: AsyncSession) -> int:
    """Create the default environments if none exist.

    Only seeded when the table is empty: environments are user-managed, and a
    team that deleted "staging" should not have it reappear on restart.
    """
    existing = int(
        (await session.execute(select(func.count(Environment.id)))).scalar() or 0
    )
    if existing:
        return 0

    for name, display, colour, order in DEFAULT_ENVIRONMENTS:
        session.add(
            Environment(
                name=name,
                display_name=display,
                color=colour,
                sort_order=order,
                is_active=True,
                description=f"{display} environment",
            )
        )
    await session.flush()
    logger.info("environments_seeded", count=len(DEFAULT_ENVIRONMENTS))
    return len(DEFAULT_ENVIRONMENTS)


async def run(session: AsyncSession) -> dict[str, int | bool]:
    """Seed roles, permissions, settings, environments and the admin user."""
    if not await _acquire_lock(session):
        logger.info("bootstrap_skipped", reason="another instance holds the lock")
        return {"skipped": True}

    try:
        roles = await user_service.ensure_roles(session)
        settings_created = await settings_service.ensure_seeded(session)
        environments_created = await seed_environments(session)
        admin, admin_created = await user_service.ensure_default_admin(session)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        try:
            await _release_lock(session)
            await session.commit()
        except Exception:  # pragma: no cover
            await session.rollback()

    if admin_created and settings.ADMIN_PASSWORD == "Passwd@123":
        logger.warning(
            "default_admin_password_in_use",
            detail=(
                "The bundled default password is in use. Change it at first "
                "sign-in and set ADMIN_PASSWORD in the environment."
            ),
        )

    result: dict[str, int | bool] = {
        "roles": len(roles),
        "settings_created": settings_created,
        "environments_created": environments_created,
        "admin_created": admin_created,
        "admin_username": admin.username,  # type: ignore[dict-item]
    }
    logger.info("bootstrap_completed", **{k: v for k, v in result.items()})
    return result
