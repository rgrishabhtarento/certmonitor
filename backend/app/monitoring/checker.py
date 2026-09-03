"""Endpoint check execution.

One :func:`run_check` call performs a complete, real probe of one endpoint and
returns a :class:`CheckOutcome`. It never touches the database - persistence,
status transitions, incidents and alerts are the caller's job (see
``app/services/monitoring_service.py``). That separation is what lets the API
run a manual "test endpoint" without writing anything, and lets the worker
reuse the identical code path.

Response bodies are deliberately not captured. Only the byte count, and an
optional substring match the operator explicitly configured, are evaluated -
monitoring must not become an accidental data-exfiltration path.
"""

from __future__ import annotations

import asyncio
import base64
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx

from app.core.config import settings
from app.core.enums import CheckStatus, CheckType, FailureReason, SslStatus
from app.core.logging import get_logger
from app.monitoring import transport as transport_module
from app.monitoring.ssl_inspect import (
    CertificateInfo,
    describe_certificate,
    extract_from_ssl_object,
    probe_tls,
)
from app.monitoring.validators import is_blocked_address

logger = get_logger(__name__)

# Headers worth keeping for diagnostics. Storing every header would bloat the
# results table and risk capturing session cookies.
_INTERESTING_HEADERS = {
    "server",
    "content-type",
    "content-length",
    "cache-control",
    "x-request-id",
    "x-correlation-id",
    "x-powered-by",
    "strict-transport-security",
    "location",
    "retry-after",
    "date",
    "via",
    "cf-ray",
    "x-amz-cf-id",
}

_MAX_BODY_PEEK_BYTES = 64 * 1024


@dataclass
class CheckTarget:
    """The subset of endpoint configuration a check needs.

    Passing a plain value object (rather than the ORM row) keeps the checker
    usable from tests and from the manual-test API without a live session.
    """

    url: str
    hostname: str
    port: int
    protocol: str = "https"
    check_type: str = CheckType.HTTP.value
    http_method: str = "GET"
    timeout_seconds: int = 10
    expected_status_codes: list[int] = field(default_factory=lambda: [200])
    expected_body_substring: str | None = None
    follow_redirects: bool = True
    verify_ssl: bool = True
    ssl_monitoring_enabled: bool = True
    request_body: str | None = None
    custom_headers: dict[str, str] | None = None
    auth_type: str = "none"
    auth_username: str | None = None
    auth_header_name: str | None = None
    auth_secret: str | None = None
    response_time_threshold_ms: int | None = None
    ssl_warning_days: int = 30
    ssl_critical_days: int = 7
    user_agent: str = "CertMonitor/1.0 (+endpoint-health-check)"


@dataclass
class CheckOutcome:
    """Result of a single probe."""

    status: str = CheckStatus.DOWN.value
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    http_status_code: int | None = None
    response_time_ms: float | None = None
    dns_time_ms: float | None = None
    connect_time_ms: float | None = None
    tls_time_ms: float | None = None
    ttfb_ms: float | None = None
    total_time_ms: float | None = None

    resolved_ip: str | None = None
    content_length: int | None = None
    redirect_count: int = 0
    final_url: str | None = None
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)
    response_headers: dict[str, str] = field(default_factory=dict)

    error_message: str | None = None
    failure_reason: str = FailureReason.NONE.value

    tls_version: str | None = None
    tls_cipher: str | None = None
    certificate: CertificateInfo | None = None

    @property
    def is_up(self) -> bool:
        return self.status in (CheckStatus.UP.value, CheckStatus.DEGRADED.value)

    @property
    def ssl_status(self) -> str | None:
        if self.certificate is None:
            return None
        return self.certificate.status

    @property
    def cert_expires_at(self) -> datetime | None:
        return self.certificate.valid_to if self.certificate else None

    @property
    def ssl_days_remaining(self) -> int | None:
        return self.certificate.days_remaining if self.certificate else None


# ------------------------------------------------------------------- DNS
async def resolve_host(
    hostname: str, port: int, *, timeout: float
) -> tuple[str | None, float, str | None]:
    """Resolve a hostname, returning ``(ip, elapsed_ms, error)``.

    An IP literal short-circuits with a zero elapsed time rather than a fake
    measurement.
    """
    loop = asyncio.get_running_loop()
    started = perf_counter()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None, (perf_counter() - started) * 1000.0, "DNS resolution timed out"
    except socket.gaierror as exc:
        return (
            None,
            (perf_counter() - started) * 1000.0,
            f"DNS resolution failed: {exc.strerror or exc}",
        )
    except OSError as exc:
        return None, (perf_counter() - started) * 1000.0, f"DNS resolution failed: {exc}"

    elapsed = (perf_counter() - started) * 1000.0
    if not infos:
        return None, elapsed, "DNS resolution returned no addresses"

    # Prefer IPv4 when both families are offered: it is what most internal
    # infrastructure actually listens on, and it keeps resolved_ip stable.
    ipv4 = next((i for i in infos if i[0] == socket.AF_INET), None)
    chosen = ipv4 or infos[0]
    return chosen[4][0], elapsed, None


# --------------------------------------------------------------- headers
def _build_headers(target: CheckTarget) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": target.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    for key, value in (target.custom_headers or {}).items():
        if key and value is not None:
            headers[str(key)] = str(value)

    secret = target.auth_secret
    if target.auth_type == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif target.auth_type == "basic" and secret is not None:
        raw = f"{target.auth_username or ''}:{secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    elif target.auth_type == "header" and secret and target.auth_header_name:
        headers[target.auth_header_name] = secret
    return headers


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    captured: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _INTERESTING_HEADERS:
            captured[lowered] = value[:512]
    return captured


def _classify_httpx_error(exc: Exception) -> tuple[str, str]:
    """Map a transport exception onto ``(failure_reason, message)``."""
    if isinstance(exc, httpx.ConnectTimeout):
        return FailureReason.CONNECTION_TIMEOUT.value, "Connection timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return FailureReason.READ_TIMEOUT.value, "Read timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return FailureReason.READ_TIMEOUT.value, "Write timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return FailureReason.CONNECTION_TIMEOUT.value, "Connection pool timeout"
    if isinstance(exc, httpx.TimeoutException):
        return FailureReason.CONNECTION_TIMEOUT.value, "Request timeout"
    if isinstance(exc, httpx.TooManyRedirects):
        return FailureReason.TOO_MANY_REDIRECTS.value, "Too many redirects"
    if isinstance(exc, httpx.ConnectError):
        message = str(exc) or "Connection failed"
        lowered = message.lower()
        if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
            return FailureReason.TLS_ERROR.value, f"TLS error: {message}"
        if "refused" in lowered:
            return FailureReason.CONNECTION_REFUSED.value, "Connection refused"
        if "name or service not known" in lowered or "nodename" in lowered:
            return FailureReason.DNS_FAILURE.value, f"DNS failure: {message}"
        return FailureReason.CONNECTION_REFUSED.value, f"Connection failed: {message}"
    if isinstance(exc, httpx.ProtocolError):
        return FailureReason.UNKNOWN_ERROR.value, f"Protocol error: {exc}"
    if isinstance(exc, httpx.TransportError):
        return FailureReason.UNKNOWN_ERROR.value, f"Transport error: {exc}"
    if isinstance(exc, httpx.InvalidURL):
        return FailureReason.CONFIG_ERROR.value, f"Invalid URL: {exc}"
    return FailureReason.UNKNOWN_ERROR.value, str(exc) or type(exc).__name__


# ---------------------------------------------------------- check runners
async def _run_http_check(target: CheckTarget, outcome: CheckOutcome) -> CheckOutcome:
    timings = transport_module.start_timing_scope()
    client = transport_module.build_async_client(
        verify=target.verify_ssl,
        timeout=float(target.timeout_seconds),
        follow_redirects=target.follow_redirects,
    )
    request_started = perf_counter()
    try:
        request = client.build_request(
            target.http_method.upper(),
            target.url,
            headers=_build_headers(target),
            content=target.request_body.encode("utf-8")
            if target.request_body
            else None,
        )
        response = await client.send(request, stream=True)
        try:
            outcome.ttfb_ms = (perf_counter() - request_started) * 1000.0
            outcome.http_status_code = response.status_code
            outcome.final_url = str(response.url)
            outcome.response_headers = _filter_response_headers(response.headers)

            # Pull TLS details from the very socket that served the request.
            network_stream = response.extensions.get("network_stream")
            if network_stream is not None:
                try:
                    server_addr = network_stream.get_extra_info("server_addr")
                    if server_addr and not outcome.resolved_ip:
                        outcome.resolved_ip = str(server_addr[0])
                except Exception:  # pragma: no cover
                    pass
                ssl_object = None
                try:
                    ssl_object = network_stream.get_extra_info("ssl_object")
                except Exception:  # pragma: no cover
                    ssl_object = None
                if ssl_object is not None:
                    leaf, chain, version, cipher = extract_from_ssl_object(ssl_object)
                    outcome.tls_version = version
                    outcome.tls_cipher = cipher
                    if leaf and target.ssl_monitoring_enabled:
                        outcome.certificate = describe_certificate(
                            leaf,
                            hostname=target.hostname,
                            warning_days=target.ssl_warning_days,
                            critical_days=target.ssl_critical_days,
                            chain_der=chain,
                            tls_version=version,
                            tls_cipher=cipher,
                            # The handshake succeeded through a verifying
                            # context, so the chain verified.
                            verified=True if target.verify_ssl else None,
                        )

            history = list(response.history)
            outcome.redirect_count = len(history)
            outcome.redirect_chain = [
                {
                    "status_code": hop.status_code,
                    "url": str(hop.url),
                    "location": hop.headers.get("location"),
                }
                for hop in history
            ]

            body_bytes = 0
            matched_substring = target.expected_body_substring is None
            peek = bytearray()
            async for chunk in response.aiter_bytes():
                body_bytes += len(chunk)
                if not matched_substring and len(peek) < _MAX_BODY_PEEK_BYTES:
                    peek.extend(chunk[: _MAX_BODY_PEEK_BYTES - len(peek)])
                    if (
                        target.expected_body_substring
                        and target.expected_body_substring.encode("utf-8", "ignore")
                        in peek
                    ):
                        matched_substring = True
                        peek.clear()
            outcome.content_length = body_bytes
        finally:
            await response.aclose()

        total_ms = (perf_counter() - request_started) * 1000.0
        outcome.total_time_ms = total_ms
        outcome.response_time_ms = total_ms

        # ------------------------------------------------- evaluate result
        if response.status_code not in target.expected_status_codes:
            outcome.status = CheckStatus.DOWN.value
            outcome.failure_reason = FailureReason.HTTP_STATUS_MISMATCH.value
            expected = ",".join(str(c) for c in target.expected_status_codes[:8])
            outcome.error_message = (
                f"Unexpected HTTP status {response.status_code} "
                f"(expected {expected})"
            )
        elif not matched_substring:
            outcome.status = CheckStatus.DOWN.value
            outcome.failure_reason = FailureReason.HTTP_STATUS_MISMATCH.value
            outcome.error_message = (
                "Response body did not contain the expected content"
            )
        else:
            threshold = target.response_time_threshold_ms
            if threshold and total_ms > threshold:
                outcome.status = CheckStatus.DEGRADED.value
                outcome.failure_reason = FailureReason.SLOW_RESPONSE.value
                outcome.error_message = (
                    f"Response time {total_ms:.0f} ms exceeded the "
                    f"{threshold} ms threshold"
                )
            else:
                outcome.status = CheckStatus.UP.value
                outcome.failure_reason = FailureReason.NONE.value

    except Exception as exc:
        reason, message = _classify_httpx_error(exc)
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = reason
        outcome.error_message = message
        outcome.total_time_ms = (perf_counter() - request_started) * 1000.0
    finally:
        outcome.connect_time_ms = timings.get("connect_time_ms")
        outcome.tls_time_ms = timings.get("tls_time_ms")
        transport_module.clear_timing_scope()
        await client.aclose()

    return outcome


async def _run_tcp_check(target: CheckTarget, outcome: CheckOutcome) -> CheckOutcome:
    """Plain TCP reachability check (no TLS, no HTTP)."""
    host = outcome.resolved_ip or target.hostname
    started = perf_counter()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=target.port),
            timeout=float(target.timeout_seconds),
        )
        elapsed = (perf_counter() - started) * 1000.0
        outcome.connect_time_ms = elapsed
        outcome.total_time_ms = elapsed
        outcome.response_time_ms = elapsed
        threshold = target.response_time_threshold_ms
        if threshold and elapsed > threshold:
            outcome.status = CheckStatus.DEGRADED.value
            outcome.failure_reason = FailureReason.SLOW_RESPONSE.value
            outcome.error_message = (
                f"Connect time {elapsed:.0f} ms exceeded the {threshold} ms threshold"
            )
        else:
            outcome.status = CheckStatus.UP.value
            outcome.failure_reason = FailureReason.NONE.value
    except asyncio.TimeoutError:
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.CONNECTION_TIMEOUT.value
        outcome.error_message = (
            f"TCP connection to {target.hostname}:{target.port} timed out"
        )
        outcome.total_time_ms = (perf_counter() - started) * 1000.0
    except ConnectionRefusedError:
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.CONNECTION_REFUSED.value
        outcome.error_message = (
            f"TCP connection to {target.hostname}:{target.port} was refused"
        )
        outcome.total_time_ms = (perf_counter() - started) * 1000.0
    except OSError as exc:
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.CONNECTION_REFUSED.value
        outcome.error_message = f"TCP connection failed: {exc}"
        outcome.total_time_ms = (perf_counter() - started) * 1000.0
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass
    return outcome


async def _run_tls_check(target: CheckTarget, outcome: CheckOutcome) -> CheckOutcome:
    """TLS-only check: handshake and certificate, no HTTP request."""
    info = await probe_tls(
        target.hostname,
        target.port,
        timeout=float(target.timeout_seconds),
        warning_days=target.ssl_warning_days,
        critical_days=target.ssl_critical_days,
        verify=target.verify_ssl,
        resolved_ip=outcome.resolved_ip,
    )
    outcome.certificate = info
    outcome.tls_version = info.tls_version
    outcome.tls_cipher = info.tls_cipher
    outcome.tls_time_ms = info.handshake_ms
    outcome.total_time_ms = info.handshake_ms
    outcome.response_time_ms = info.handshake_ms

    if info.status == SslStatus.EXPIRED.value:
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.CERT_EXPIRED.value
        outcome.error_message = "Certificate has expired"
    elif info.error or info.status in (
        SslStatus.UNABLE_TO_CHECK.value,
        SslStatus.INVALID.value,
    ):
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = (
            FailureReason.CERT_INVALID.value
            if info.status == SslStatus.INVALID.value
            else FailureReason.TLS_ERROR.value
        )
        outcome.error_message = (
            info.error or info.verification_error or "TLS check failed"
        )
    else:
        outcome.status = CheckStatus.UP.value
        outcome.failure_reason = FailureReason.NONE.value
    return outcome


async def run_check(target: CheckTarget) -> CheckOutcome:
    """Execute a full check: DNS, transport, protocol and certificate."""
    outcome = CheckOutcome()

    # ------------------------------------------------------------- DNS
    ip, dns_ms, dns_error = await resolve_host(
        target.hostname, target.port, timeout=float(target.timeout_seconds)
    )
    outcome.dns_time_ms = round(dns_ms, 3)
    outcome.resolved_ip = ip
    if dns_error:
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.DNS_FAILURE.value
        outcome.error_message = dns_error
        outcome.total_time_ms = outcome.dns_time_ms
        return outcome

    if ip:
        blocked, why = is_blocked_address(ip)
        if blocked:
            outcome.status = CheckStatus.DOWN.value
            outcome.failure_reason = FailureReason.BLOCKED_TARGET.value
            outcome.error_message = f"Refusing to check {target.hostname} ({ip}): {why}"
            return outcome

    # -------------------------------------------------------- protocol
    if target.check_type == CheckType.TCP.value:
        await _run_tcp_check(target, outcome)
    elif target.check_type == CheckType.TLS.value:
        await _run_tls_check(target, outcome)
    else:
        await _run_http_check(target, outcome)

    # ------------------------------------- certificate fallback for HTTPS
    # If the HTTP request failed before a handshake was observed, or the
    # runtime gave us no SSL object, inspect the certificate separately so an
    # expiring cert on a currently-failing endpoint is still reported.
    needs_cert = (
        target.check_type == CheckType.HTTP.value
        and target.protocol == "https"
        and target.ssl_monitoring_enabled
        and outcome.certificate is None
    )
    if needs_cert:
        info = await probe_tls(
            target.hostname,
            target.port,
            timeout=float(target.timeout_seconds),
            warning_days=target.ssl_warning_days,
            critical_days=target.ssl_critical_days,
            verify=target.verify_ssl,
            resolved_ip=outcome.resolved_ip,
        )
        outcome.certificate = info
        outcome.tls_version = outcome.tls_version or info.tls_version
        outcome.tls_cipher = outcome.tls_cipher or info.tls_cipher
        if outcome.tls_time_ms is None:
            outcome.tls_time_ms = info.handshake_ms

    # An expired certificate on a verifying HTTPS check is a hard failure even
    # if the server somehow answered.
    if (
        outcome.certificate
        and outcome.certificate.status == SslStatus.EXPIRED.value
        and target.verify_ssl
        and outcome.status == CheckStatus.UP.value
    ):
        outcome.status = CheckStatus.DOWN.value
        outcome.failure_reason = FailureReason.CERT_EXPIRED.value
        outcome.error_message = "TLS certificate has expired"

    for attr in ("dns_time_ms", "connect_time_ms", "tls_time_ms", "ttfb_ms",
                 "total_time_ms", "response_time_ms"):
        value = getattr(outcome, attr)
        if value is not None:
            setattr(outcome, attr, round(float(value), 3))

    return outcome


def build_target_from_endpoint(endpoint: Any, *, auth_secret: str | None = None,
                               defaults: dict[str, Any] | None = None) -> CheckTarget:
    """Adapt an ``Endpoint`` ORM row into a :class:`CheckTarget`."""
    defaults = defaults or {}
    warning_days = endpoint.ssl_warning_days or defaults.get(
        "ssl_warning_days", settings.SSL_WARNING_DAYS
    )
    critical_days = endpoint.ssl_critical_days or defaults.get(
        "ssl_critical_days", settings.SSL_CRITICAL_DAYS
    )
    threshold = endpoint.response_time_threshold_ms
    if threshold is None:
        threshold = defaults.get(
            "response_time_threshold_ms", settings.RESPONSE_TIME_THRESHOLD_MS
        )
    return CheckTarget(
        url=endpoint.url,
        hostname=endpoint.hostname,
        port=endpoint.port,
        protocol=endpoint.protocol,
        check_type=endpoint.check_type,
        http_method=endpoint.http_method,
        timeout_seconds=endpoint.timeout_seconds,
        expected_status_codes=endpoint.expected_status_list,
        expected_body_substring=endpoint.expected_body_substring,
        follow_redirects=endpoint.follow_redirects,
        verify_ssl=endpoint.verify_ssl,
        ssl_monitoring_enabled=endpoint.ssl_monitoring_enabled
        and endpoint.protocol == "https",
        request_body=endpoint.request_body,
        custom_headers=endpoint.custom_headers or {},
        auth_type=endpoint.auth_type,
        auth_username=endpoint.auth_username,
        auth_header_name=endpoint.auth_header_name,
        auth_secret=auth_secret,
        response_time_threshold_ms=threshold,
        ssl_warning_days=int(warning_days),
        ssl_critical_days=int(critical_days),
    )
