"""Shared test fixtures.

The suite runs against a throwaway SQLite file rather than PostgreSQL so it
needs no services. The schema is created from the same declarative metadata the
migration was written from, and the JSON/BigInteger columns carry SQLite
variants for exactly this reason.

Environment variables are set *before* the application package is imported,
because settings are read once at import time.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------- env setup
_TMP_DIR = tempfile.mkdtemp(prefix="infrasight-tests-")
_DB_PATH = Path(_TMP_DIR) / "test.sqlite3"

os.environ.update(
    {
        "APP_ENV": "testing",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}",
        # No Redis in the test environment: the rate limiter and preview store
        # fall back to their in-process implementations.
        "REDIS_URL": "",
        "JWT_SECRET": "test-secret-key-that-is-long-enough-for-tests-0123456789",
        "ENCRYPTION_KEY": "test-encryption-key-material-for-the-suite",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "Passwd@123",
        "ADMIN_EMAIL": "admin@example.test",
        "ADMIN_FORCE_PASSWORD_CHANGE": "false",
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "console",
        "MIN_MONITOR_INTERVAL": "30",
        "DEFAULT_MONITOR_INTERVAL": "60",
        "DEFAULT_TIMEOUT": "5",
        "WORKER_ENABLED": "false",
        # Allow loopback so a test can point a check at a local server.
        "ALLOW_LOOPBACK_TARGETS": "true",
        "LOGIN_RATE_LIMIT_ATTEMPTS": "50",
        "ACCOUNT_LOCKOUT_ATTEMPTS": "5",
    }
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.core.database import SessionFactory, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import settings_service, user_service  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _event_loop_policy():
    """Keep the default policy; declared so the fixture ordering is explicit."""
    yield


@pytest.fixture(autouse=True)
async def database():
    """Fresh schema per test.

    Dropping and recreating is fast on SQLite and guarantees no cross-test
    leakage of endpoints, incidents or alerts.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    settings_service.invalidate_cache()

    yield

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    settings_service.invalidate_cache()


@pytest.fixture
async def session():
    """A session for tests that talk to the database directly."""
    async with SessionFactory() as db_session:
        yield db_session


@pytest.fixture
async def seeded(session):
    """Roles, permissions, default settings and the admin account."""
    roles = await user_service.ensure_roles(session)
    await settings_service.ensure_seeded(session)
    admin, _ = await user_service.ensure_default_admin(session)
    await session.commit()
    return {"roles": roles, "admin": admin}


@pytest.fixture
async def viewer(session, seeded):
    """A viewer account, for authorisation tests."""
    user = await user_service.create_user(
        session,
        username="viewer1",
        password="ViewerPass@123",
        role_name="viewer",
        email="viewer@example.test",
        must_change_password=False,
    )
    await session.commit()
    return user


@pytest.fixture
async def client(seeded):
    """HTTP client bound to the ASGI app.

    The app's lifespan is not run, so bootstrap is provided by the ``seeded``
    fixture instead - that keeps each test's setup explicit.
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


async def authenticate(client: httpx.AsyncClient, username: str, password: str) -> str:
    """Sign in and return the access token."""
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
async def admin_token(client):
    return await authenticate(client, "admin", "Passwd@123")


@pytest.fixture
async def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
async def viewer_token(client, viewer):
    return await authenticate(client, "viewer1", "ViewerPass@123")


@pytest.fixture
async def viewer_headers(viewer_token):
    return {"Authorization": f"Bearer {viewer_token}"}


@pytest.fixture
async def runtime_config(session, seeded):
    return await settings_service.load_settings(session, use_cache=False)


@pytest.fixture
async def endpoint_factory(session, seeded, runtime_config):
    """Create endpoints directly, bypassing the API."""
    from app.services import endpoint_service

    created = []

    async def _create(**overrides):
        payload = {
            "name": overrides.pop("name", f"Endpoint {uuid.uuid4().hex[:6]}"),
            "url": overrides.pop("url", f"https://{uuid.uuid4().hex[:8]}.example.com/health"),
        }
        payload.update(overrides)
        endpoint = await endpoint_service.create_endpoint(
            session,
            payload,
            config=runtime_config,
            created_by_id=None,
        )
        await session.commit()
        created.append(endpoint)
        return endpoint

    return _create


@pytest.fixture
def password_hash():
    return hash_password
