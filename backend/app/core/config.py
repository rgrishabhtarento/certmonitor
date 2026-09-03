"""Application configuration.

Every environment-specific value is read from environment variables so that the
same image can be promoted across dev/staging/production without a rebuild.
Nothing sensitive is defaulted to a real value.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "CertMonitor"
    APP_ENV: Literal["development", "testing", "staging", "production"] = "production"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    API_PREFIX: str = "/api"
    ROOT_PATH: str = ""

    # CORS. In production the frontend is served by the same nginx that proxies
    # /api, so no cross-origin request happens and this can stay empty.
    # Typed as a plain string, NOT list[str], on purpose. pydantic-settings
    # classifies a list-annotated field as "complex" and runs json.loads() on
    # the raw environment value inside the settings source - which happens
    # before any field_validator can intervene. An empty or comma-separated
    # value would therefore raise JSONDecodeError at import time. Parsing is
    # done by the `cors_origins` property instead.
    CORS_ORIGINS: str = ""

    # Comma-separated Host allow-list. Leave empty when a reverse proxy
    # terminates requests (the default Compose topology).
    ALLOWED_HOSTS: str = ""

    # ------------------------------------------------------------- database
    DATABASE_URL: str = "postgresql+asyncpg://certmonitor:certmonitor@postgres:5432/certmonitor"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_ECHO: bool = False

    # ---------------------------------------------------------------- redis
    REDIS_URL: str | None = "redis://redis:6379/0"

    # ------------------------------------------------------------- security
    # JWT_SECRET must be provided in any real deployment; a random value is
    # generated as a last resort so a dev container still boots (tokens then
    # become invalid on restart, which is intentional and loud).
    JWT_SECRET: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Key used to encrypt endpoint credentials at rest. Derived from JWT_SECRET
    # when not supplied explicitly.
    ENCRYPTION_KEY: str | None = None

    PASSWORD_MIN_LENGTH: int = 10

    LOGIN_RATE_LIMIT_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 300
    ACCOUNT_LOCKOUT_ATTEMPTS: int = 8
    ACCOUNT_LOCKOUT_MINUTES: int = 15

    SECURE_COOKIES: bool = True
    HSTS_ENABLED: bool = True

    # -------------------------------------------------------- initial admin
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "Passwd@123"
    ADMIN_EMAIL: str = "admin@localhost"
    ADMIN_FORCE_PASSWORD_CHANGE: bool = True

    # ------------------------------------------------------------ monitoring
    DEFAULT_MONITOR_INTERVAL: int = 60
    DEFAULT_TIMEOUT: int = 10
    MIN_MONITOR_INTERVAL: int = 30
    MAX_MONITOR_INTERVAL: int = 86400

    SSL_WARNING_DAYS: int = 30
    SSL_CRITICAL_DAYS: int = 7

    FAILURE_THRESHOLD: int = 3
    RESPONSE_TIME_THRESHOLD_MS: int = 2000
    ALERT_COOLDOWN_MINUTES: int = 30
    DATA_RETENTION_DAYS: int = 90

    # ---------------------------------------------------------- worker knobs
    WORKER_ENABLED: bool = True
    WORKER_CONCURRENCY: int = 50
    WORKER_POLL_INTERVAL_SECONDS: int = 5
    WORKER_BATCH_SIZE: int = 200
    WORKER_ID: str | None = None
    WORKER_HEARTBEAT_SECONDS: int = 15
    # A worker is considered unhealthy by /health if it has not written a
    # heartbeat within this many seconds.
    WORKER_STALE_AFTER_SECONDS: int = 90
    RETENTION_SWEEP_INTERVAL_SECONDS: int = 3600

    # Safety valve: refuse to check hosts that resolve to link-local/loopback
    # ranges unless explicitly permitted (private RFC1918 ranges stay allowed
    # because monitoring internal infrastructure is the primary use case).
    ALLOW_LOOPBACK_TARGETS: bool = False

    MAX_IMPORT_ROWS: int = 5000
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024

    # ------------------------------------------------------------ validators
    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalise_async_dsn(cls, value: Any) -> Any:
        """Accept the plain ``postgresql://`` DSN operators usually paste."""
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if isinstance(value, str) and value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        return value

    @model_validator(mode="after")
    def _check_production_hardening(self) -> "Settings":
        if self.MIN_MONITOR_INTERVAL < 10:
            raise ValueError("MIN_MONITOR_INTERVAL must be >= 10 seconds")
        if self.DEFAULT_MONITOR_INTERVAL < self.MIN_MONITOR_INTERVAL:
            self.DEFAULT_MONITOR_INTERVAL = self.MIN_MONITOR_INTERVAL
        if self.SSL_CRITICAL_DAYS > self.SSL_WARNING_DAYS:
            raise ValueError("SSL_CRITICAL_DAYS must be <= SSL_WARNING_DAYS")
        return self

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _split_csv(raw: str) -> list[str]:
        """Split a comma-separated env value, tolerating blanks and spaces."""
        return [item.strip() for item in (raw or "").split(",") if item.strip()]

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins.

        Accepts a comma-separated list (``https://a.com,https://b.com``) or a
        JSON array, and treats an empty value as "no cross-origin requests" -
        which is correct for the bundled deployment, where nginx serves the SPA
        and proxies /api on the same origin.
        """
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw:
            return []
        if raw.startswith("["):
            import json

            try:
                parsed = json.loads(raw)
            except ValueError:
                return self._split_csv(raw.strip("[]"))
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            return []
        return self._split_csv(raw)

    @property
    def allowed_hosts(self) -> list[str]:
        """Host allow-list; empty means accept any Host header."""
        return self._split_csv(self.ALLOWED_HOSTS)

    @property
    def sync_database_url(self) -> str:
        """Sync DSN used by Alembic migrations."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg2")

    @property
    def is_production(self) -> bool:
        return self.APP_ENV in ("production", "staging")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
