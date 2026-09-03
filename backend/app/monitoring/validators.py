"""URL/target validation and parsing.

Used by the API schemas (reject bad input early), the CSV importer, and the
worker (refuse to probe targets that should never be probed).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings

ALLOWED_SCHEMES = {"http", "https", "tcp", "tls"}
DEFAULT_PORTS = {"http": 80, "https": 443, "tcp": 443, "tls": 443}

# RFC 1123 hostname label rules.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


class UrlValidationError(ValueError):
    """Raised when a target cannot be monitored as written."""


@dataclass(frozen=True)
class ParsedTarget:
    url: str
    protocol: str
    hostname: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        default = DEFAULT_PORTS.get(self.protocol)
        if self.port == default:
            return f"{self.protocol}://{self.hostname}"
        return f"{self.protocol}://{self.hostname}:{self.port}"


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def parse_target(raw: str, *, default_scheme: str = "https") -> ParsedTarget:
    """Normalise a user-supplied endpoint into its component parts.

    Accepts what people actually type: ``example.com``,
    ``https://api.example.com/health``, ``http://10.10.10.10:8080/health``,
    ``https://api.example.com:8443/status``.
    """
    if raw is None:
        raise UrlValidationError("URL is required")

    candidate = raw.strip()
    if not candidate:
        raise UrlValidationError("URL is required")
    if len(candidate) > 2048:
        raise UrlValidationError("URL must be at most 2048 characters")
    if any(ch in candidate for ch in ("\n", "\r", "\t", " ")):
        raise UrlValidationError("URL must not contain whitespace or control characters")

    if "://" not in candidate:
        candidate = f"{default_scheme}://{candidate}"

    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError(
            f"unsupported scheme '{parts.scheme}'; expected one of "
            + ", ".join(sorted(ALLOWED_SCHEMES))
        )

    if parts.username or parts.password:
        # Credentials in the URL would end up in logs and audit trails; the
        # endpoint's authentication fields are the supported route.
        raise UrlValidationError(
            "credentials must not be embedded in the URL - use the "
            "authentication settings instead"
        )

    host = parts.hostname
    if not host:
        raise UrlValidationError("URL is missing a hostname")
    host = host.lower().rstrip(".")

    if not _looks_like_ip(host) and not _HOSTNAME_RE.match(host):
        raise UrlValidationError(f"'{host}' is not a valid hostname or IP address")

    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:  # urlsplit raises for a non-numeric port
        raise UrlValidationError("port must be a number between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise UrlValidationError("port must be between 1 and 65535")

    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if parts.query:
        path = f"{path}?{parts.query}"

    normalised = urlunsplit(
        (
            scheme,
            f"{host}:{port}"
            if port != DEFAULT_PORTS[scheme]
            else host,
            parts.path or "/",
            parts.query,
            "",
        )
    )

    return ParsedTarget(
        url=normalised,
        protocol=scheme,
        hostname=host,
        port=port,
        path=path,
    )


def validate_url(raw: str) -> str:
    """Return the normalised URL, raising ``UrlValidationError`` if unusable."""
    return parse_target(raw).url


def is_blocked_address(ip: str) -> tuple[bool, str | None]:
    """Decide whether an already-resolved address may be probed.

    Private RFC1918 space is explicitly allowed - monitoring internal
    infrastructure is a primary use case for this tool. Loopback, link-local
    (including the cloud metadata range) and unspecified addresses are refused
    unless ALLOW_LOOPBACK_TARGETS is set, because a check against them is
    almost always a misconfiguration or an attempt to reach the container
    itself.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True, f"'{ip}' is not a valid IP address"

    if settings.ALLOW_LOOPBACK_TARGETS:
        return False, None

    if address.is_loopback:
        return True, "loopback addresses are not monitored"
    if address.is_link_local:
        return True, "link-local addresses (including cloud metadata) are not monitored"
    if address.is_unspecified:
        return True, "unspecified addresses are not monitored"
    if address.is_multicast:
        return True, "multicast addresses are not monitored"
    if address.is_reserved:
        return True, "reserved addresses are not monitored"
    return False, None


def normalise_status_codes(raw: str | None, *, default: str = "200") -> str:
    """Validate and canonicalise an expected-status specification.

    Accepts ``200``, ``200,204``, ``2xx`` and ranges like ``200-299``.
    """
    if raw is None or not str(raw).strip():
        return default

    tokens: list[str] = []
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        if re.fullmatch(r"[1-5]xx", chunk):
            base = int(chunk[0]) * 100
            tokens.extend(str(code) for code in range(base, base + 100))
            continue
        range_match = re.fullmatch(r"(\d{3})\s*-\s*(\d{3})", chunk)
        if range_match:
            low, high = int(range_match.group(1)), int(range_match.group(2))
            if low > high:
                raise UrlValidationError(f"invalid status range '{chunk}'")
            if high - low > 200:
                raise UrlValidationError(f"status range '{chunk}' is too wide")
            tokens.extend(str(code) for code in range(low, high + 1))
            continue
        if not re.fullmatch(r"\d{3}", chunk):
            raise UrlValidationError(
                f"'{chunk}' is not a valid HTTP status code, range or class"
            )
        code = int(chunk)
        if not 100 <= code <= 599:
            raise UrlValidationError(f"HTTP status {code} is out of range")
        tokens.append(chunk)

    if not tokens:
        return default

    unique = sorted({int(t) for t in tokens})
    result = ",".join(str(code) for code in unique)
    if len(result) > 128:
        # Keep it storable; a class like "2xx" expands past the column width.
        raise UrlValidationError(
            "expected status specification is too long - use fewer codes"
        )
    return result


def clamp_interval(value: int | None) -> int:
    """Keep intervals inside the configured safety band.

    Aggressive intervals can turn a monitor into a load generator, so the lower
    bound is enforced server-side rather than trusted from the client.
    """
    if value is None:
        return settings.DEFAULT_MONITOR_INTERVAL
    return max(settings.MIN_MONITOR_INTERVAL, min(int(value), settings.MAX_MONITOR_INTERVAL))


def clamp_timeout(value: int | None, *, interval: int | None = None) -> int:
    if value is None:
        value = settings.DEFAULT_TIMEOUT
    value = max(1, min(int(value), 120))
    # A timeout longer than the interval guarantees overlapping checks.
    if interval:
        value = min(value, max(1, int(interval)))
    return value
