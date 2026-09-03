"""HTTP transport instrumented with per-phase timings.

httpx reports a total duration but not how that time split between connecting
and the TLS handshake. Those phases are where most real-world latency problems
live, so we wrap httpcore's network backend to time them on the very connection
the request uses - no second probe, no extra load on the monitored host.

The wrapper leans on httpcore's public backend interface. If a future httpcore
changes that interface, :func:`build_async_client` falls back to the stock
transport and the sub-timings are simply reported as ``None``; the check itself
keeps working.
"""

from __future__ import annotations

import typing
from contextvars import ContextVar
from time import perf_counter

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

# Populated by the instrumented backend, read by the checker after the request.
_phase_timings: ContextVar[dict[str, float] | None] = ContextVar(
    "phase_timings", default=None
)

_BACKEND_AVAILABLE = True
try:  # pragma: no cover - exercised implicitly by the worker
    import httpcore

    _AnyIOBackend = httpcore.AnyIOBackend
    _AsyncNetworkStream = httpcore.AsyncNetworkStream
except Exception:  # pragma: no cover - degraded mode
    _BACKEND_AVAILABLE = False
    httpcore = None  # type: ignore[assignment]
    _AnyIOBackend = object  # type: ignore[assignment,misc]
    _AsyncNetworkStream = object  # type: ignore[assignment,misc]


def start_timing_scope() -> dict[str, float]:
    """Begin collecting phase timings for the current task."""
    timings: dict[str, float] = {}
    _phase_timings.set(timings)
    return timings


def clear_timing_scope() -> None:
    _phase_timings.set(None)


def _record(name: str, value: float) -> None:
    sink = _phase_timings.get()
    if sink is not None:
        # Keep the first observation: a redirect chain would otherwise
        # overwrite the timings of the initial connection.
        sink.setdefault(name, value)


if _BACKEND_AVAILABLE:

    class _TimingStream(_AsyncNetworkStream):  # type: ignore[misc,valid-type]
        """Passthrough stream that times ``start_tls``."""

        def __init__(self, inner: typing.Any) -> None:
            self._inner = inner

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            return await self._inner.read(max_bytes, timeout)

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            await self._inner.write(buffer, timeout)

        async def aclose(self) -> None:
            await self._inner.aclose()

        async def start_tls(
            self,
            ssl_context: typing.Any,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> typing.Any:
            started = perf_counter()
            inner = await self._inner.start_tls(ssl_context, server_hostname, timeout)
            _record("tls_time_ms", (perf_counter() - started) * 1000.0)
            return _TimingStream(inner)

        def get_extra_info(self, info: str) -> typing.Any:
            return self._inner.get_extra_info(info)

    class TimingBackend(_AnyIOBackend):  # type: ignore[misc,valid-type]
        """Network backend that times TCP connect and TLS handshake."""

        async def connect_tcp(  # type: ignore[override]
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: typing.Any = None,
        ) -> typing.Any:
            started = perf_counter()
            stream = await super().connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
            _record("connect_time_ms", (perf_counter() - started) * 1000.0)
            return _TimingStream(stream)

        async def connect_unix_socket(  # type: ignore[override]
            self,
            path: str,
            timeout: float | None = None,
            socket_options: typing.Any = None,
        ) -> typing.Any:
            stream = await super().connect_unix_socket(
                path, timeout=timeout, socket_options=socket_options
            )
            return _TimingStream(stream)


def build_async_client(
    *,
    verify: bool,
    timeout: float,
    follow_redirects: bool,
    max_redirects: int = 10,
    http2: bool = False,
) -> httpx.AsyncClient:
    """Create a single-use client for one endpoint check.

    Connections are not pooled across checks on purpose: a monitor should
    measure a cold, representative request rather than reuse a warm keep-alive
    socket that hides connection-level problems.
    """
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
    timeout_config = httpx.Timeout(timeout, connect=timeout, read=timeout, write=timeout)

    transport: httpx.AsyncBaseTransport | None = None
    if _BACKEND_AVAILABLE:
        try:
            transport = _InstrumentedTransport(
                verify=verify,
                http2=http2,
                limits=limits,
            )
        except Exception as exc:  # pragma: no cover - degraded mode
            logger.debug("timing_transport_unavailable", error=str(exc))
            transport = None

    return httpx.AsyncClient(
        transport=transport,
        verify=verify,
        timeout=timeout_config,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        limits=limits,
        http2=http2,
        trust_env=False,
    )


if _BACKEND_AVAILABLE:

    class _InstrumentedTransport(httpx.AsyncHTTPTransport):
        """``AsyncHTTPTransport`` whose pool uses :class:`TimingBackend`."""

        def __init__(
            self,
            *,
            verify: bool,
            http2: bool,
            limits: httpx.Limits,
        ) -> None:
            super().__init__(verify=verify, http2=http2, limits=limits, retries=0)
            ssl_context = httpx.create_ssl_context(verify=verify)
            self._pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=True,
                http2=http2,
                retries=0,
                network_backend=TimingBackend(),
            )
