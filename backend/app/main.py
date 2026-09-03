"""FastAPI application factory.

Wires middleware, exception handlers, OpenAPI metadata and the startup
sequence. The monitoring worker is a separate process (see
``app/workers/monitor_worker.py``); nothing in this module performs endpoint
checks, so a slow or unreachable monitored host can never delay an API
request.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.health import APP_VERSION, router as health_router
from app.api.v1 import api_router
from app.core.config import settings
from app.core.database import SessionFactory, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import limiter
from app.monitoring.validators import UrlValidationError

configure_logging()
logger = get_logger(__name__)

DESCRIPTION = """
Infrastructure endpoint and SSL certificate monitoring.

**Authentication.** Every route except `/health`, `/ready`, `/live` and
`/api/auth/login` requires a bearer token obtained from
`POST /api/auth/login`. Send it as `Authorization: Bearer <access_token>`.

**Roles.** `admin` can change configuration, manage users and manage
endpoints. `viewer` is read-only.

**Monitoring data.** Everything returned by the dashboard, SSL and history
routes comes from checks actually executed by the monitoring worker.
"""

TAGS_METADATA = [
    {"name": "Authentication", "description": "Sign in, refresh and password changes."},
    {"name": "Endpoints", "description": "Manage monitored endpoints, run checks, read history."},
    {"name": "Dashboard", "description": "Aggregated health, availability and trends."},
    {"name": "SSL Certificates", "description": "Certificate inventory and expiry tracking."},
    {"name": "Incidents & Alerts", "description": "Outage records and generated alerts."},
    {"name": "Tags & Environments", "description": "Categorisation of endpoints."},
    {"name": "Users", "description": "User and role administration."},
    {"name": "Settings", "description": "Runtime configuration, audit log, notification channels."},
    {"name": "Import & Export", "description": "Bulk CSV/Excel import and configuration export."},
    {"name": "Health", "description": "Probes for Docker and Kubernetes."},
]


# ------------------------------------------------------------- middleware
class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, bind log context and emit one access line."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000.0
            logger.exception(
                "request_failed", duration_ms=round(duration_ms, 2)
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"

        # Probes are polled constantly; logging them would drown the signal.
        if request.url.path not in ("/health", "/ready", "/live", "/metrics"):
            user = getattr(request.state, "user", None)
            logger.info(
                "request",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
                user=getattr(user, "username", None),
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply defensive response headers.

    The API returns only JSON, so a restrictive CSP costs nothing here; the
    frontend container sets its own policy for the HTML it serves.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.HSTS_ENABLED and settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


# --------------------------------------------------------------- lifespan
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "api_starting",
        version=APP_VERSION,
        environment=settings.APP_ENV,
        worker_enabled=settings.WORKER_ENABLED,
    )

    if settings.is_production and settings.JWT_SECRET and len(settings.JWT_SECRET) < 32:
        logger.warning(
            "weak_jwt_secret",
            detail="Set JWT_SECRET to at least 32 random characters.",
        )

    from app import bootstrap

    try:
        async with SessionFactory() as session:
            await bootstrap.run(session)
    except Exception as exc:
        # Do not crash-loop the container: /ready keeps reporting not-ready,
        # which is the signal an operator needs while migrations catch up.
        logger.error(
            "bootstrap_failed",
            error=str(exc),
            detail="the API will report not-ready until this is resolved",
        )

    try:
        yield
    finally:
        logger.info("api_stopping")
        await limiter.close()
        await dispose_engine()


# ------------------------------------------------------------ application
def create_app() -> FastAPI:
    app = FastAPI(
        title="CertMonitor API",
        description=DESCRIPTION,
        version=APP_VERSION,
        openapi_tags=TAGS_METADATA,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        root_path=settings.ROOT_PATH,
        lifespan=lifespan,
        contact={"name": "Platform Engineering"},
        license_info={"name": "Proprietary"},
    )

    # Order is outermost-first: security headers wrap everything, then the
    # request-context logger, then compression.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    cors_origins = settings.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Response-Time-Ms"],
            max_age=600,
        )

    # In the default deployment nginx terminates requests and proxies to the
    # API by service name, so Host is trusted. Operators exposing the API
    # directly should set ALLOWED_HOSTS.
    allowed_hosts = settings.allowed_hosts
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_PREFIX)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Turn Pydantic errors into a flat field->message map.

        The default FastAPI payload is awkward to render in a form; this shape
        maps directly onto per-field messages in the UI.
        """
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", []) if part != "body"]
            key = ".".join(location) or "body"
            fields[key] = error.get("msg", "invalid value")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "The request could not be validated.",
                "code": "validation_error",
                "fields": fields,
            },
        )

    @app.exception_handler(UrlValidationError)
    async def url_validation_handler(
        request: Request, exc: UrlValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc), "code": "invalid_url"},
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        """Report a constraint violation without leaking SQL.

        The driver's message can contain table and column names, so only a
        generic conflict is returned to the client; the detail goes to the log.
        """
        logger.warning("integrity_error", error=str(exc.orig) if exc.orig else str(exc))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    "The change conflicts with existing data. It may already "
                    "exist, or another user changed it first."
                ),
                "code": "conflict",
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        logger.error("database_error", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": "A database error occurred. Please try again.",
                "code": "database_error",
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Last resort.

        Never echoes the exception text: a stack trace or driver message in an
        API response is an information leak. The request id ties the response
        back to the full detail in the log.
        """
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An unexpected error occurred.",
                "code": "internal_error",
                "request_id": request_id,
            },
        )


app = create_app()
