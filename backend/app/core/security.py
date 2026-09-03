"""Password hashing, JWT issuing/verification and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from app.core.config import settings

# bcrypt has a hard 72-byte input limit; rather than let passlib silently
# truncate, password validation rejects anything longer.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TokenType = Literal["access", "refresh"]


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        # Malformed hash stored in the row: treat as a failed login rather
        # than a 500.
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return pwd_context.needs_update(hashed)
    except ValueError:
        return True


_PASSWORD_RULES = (
    (re.compile(r"[a-z]"), "one lowercase letter"),
    (re.compile(r"[A-Z]"), "one uppercase letter"),
    (re.compile(r"[0-9]"), "one digit"),
    (re.compile(r"[^A-Za-z0-9]"), "one special character"),
)


def validate_password_strength(password: str) -> list[str]:
    """Return human-readable problems; an empty list means acceptable."""
    problems: list[str] = []
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        problems.append(
            f"must be at least {settings.PASSWORD_MIN_LENGTH} characters long"
        )
    if len(password.encode("utf-8")) > 72:
        problems.append("must be at most 72 bytes long")
    for pattern, description in _PASSWORD_RULES:
        if not pattern.search(password):
            problems.append(f"must contain at least {description}")
    return problems


# --------------------------------------------------------------------- JWT
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    role: str | None = None,
    token_version: int = 0,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    if expires_delta is None:
        expires_delta = (
            timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
            if token_type == "access"
            else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        )
    issued_at = _now()
    expires_at = issued_at + expires_delta
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "tv": token_version,
    }
    if role:
        payload["role"] = role
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    """Decode and validate a JWT. Raises ``jwt.PyJWTError`` on any problem."""
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        options={"require": ["exp", "sub", "type"]},
    )
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected a {expected_type} token")
    return payload


# -------------------------------------------------------- credential crypto
def _fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if key:
        # Accept either a ready-made Fernet key or arbitrary key material.
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            pass
        material = key.encode("utf-8")
    else:
        material = settings.JWT_SECRET.encode("utf-8")
    derived = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(derived)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str | None:
    """Return the plaintext, or ``None`` when the value cannot be decrypted.

    ``None`` normally means ENCRYPTION_KEY/JWT_SECRET was rotated. The worker
    then records an explicit failure reason instead of crashing.
    """
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def mask_secret(value: str | None, *, keep: int = 4) -> str | None:
    """Produce a display-safe hint such as ``****abcd``."""
    if not value:
        return None
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(4, len(value) - keep) + value[-keep:]
