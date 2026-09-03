"""Failure triage for a single endpoint.

Answers the question an operator actually has when something goes red: *which
layer is broken, and what do I look at next?*

The approach is layered isolation. A request to an HTTPS endpoint passes
through four stages, and each can fail independently:

    DNS  ->  TCP  ->  TLS  ->  HTTP

By probing them separately we can report the deepest stage that succeeded,
which localises the fault immediately. "TLS handshake fine, HTTP 503" is a
completely different problem from "TCP refused", even though both surface as
DOWN in the dashboard.

Three sources of evidence are combined:

* **Live probes** - run now, per stage, including per-address TCP so a single
  bad backend behind a load balancer is visible.
* **Stored history** - the distribution of failure reasons and status codes,
  whether the endpoint has *ever* succeeded, and when it last did.
* **Correlation** - whether sibling endpoints on the same host or in the same
  environment are also failing, which separates "this service" from "this
  network".

Everything here is read-only apart from the outbound probes; nothing is
written to the monitoring history, so diagnosing an endpoint never distorts
its uptime figures.
"""

from __future__ import annotations

import asyncio
import socket
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import CheckStatus, EndpointStatus
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.models.endpoint import Endpoint
from app.models.monitoring import MonitoringResult
from app.monitoring import transport as transport_module
from app.monitoring.checker import build_headers, build_target_from_endpoint
from app.monitoring.ssl_inspect import probe_tls
from app.monitoring.validators import is_blocked_address

logger = get_logger(__name__)

# Diagnostics runs several probes back to back, so each one is kept short -
# an operator is waiting on the response.
PROBE_TIMEOUT = 8.0
# Cap the per-address TCP fan-out: enough to spot one bad backend, bounded so
# a host with 20 A records cannot stall the request.
MAX_ADDRESSES = 4
HISTORY_WINDOW_HOURS = 24
HISTORY_SAMPLE = 200

OK = "ok"
FAILED = "failed"
WARNING = "warning"
SKIPPED = "skipped"


@dataclass
class Layer:
    layer: str
    status: str
    detail: str
    elapsed_ms: float | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    severity: str          # high | medium | low
    title: str
    detail: str
    action: str


# --------------------------------------------------------------- DNS stage
async def _dns_stage(hostname: str, port: int) -> Layer:
    """Resolve every address, not just the one a check would use."""
    if _is_ip_literal(hostname):
        return Layer(
            layer="dns",
            status=SKIPPED,
            detail=f"{hostname} is an IP literal - no resolution needed",
            data={"addresses": [hostname]},
        )

    loop = asyncio.get_running_loop()
    started = perf_counter()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
            timeout=PROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return Layer(
            layer="dns",
            status=FAILED,
            detail=f"DNS resolution timed out after {PROBE_TIMEOUT:g}s",
            elapsed_ms=round((perf_counter() - started) * 1000, 1),
        )
    except socket.gaierror as exc:
        return Layer(
            layer="dns",
            status=FAILED,
            detail=f"DNS resolution failed: {exc.strerror or exc}",
            elapsed_ms=round((perf_counter() - started) * 1000, 1),
        )
    except OSError as exc:
        return Layer(
            layer="dns",
            status=FAILED,
            detail=f"DNS resolution failed: {exc}",
            elapsed_ms=round((perf_counter() - started) * 1000, 1),
        )

    elapsed = round((perf_counter() - started) * 1000, 1)
    ipv4, ipv6 = [], []
    for info in infos:
        address = info[4][0]
        (ipv4 if info[0] == socket.AF_INET else ipv6).append(address)
    ordered = list(dict.fromkeys(ipv4 + ipv6))

    if not ordered:
        return Layer(
            layer="dns", status=FAILED,
            detail="resolver returned no addresses", elapsed_ms=elapsed,
        )

    return Layer(
        layer="dns",
        status=OK,
        detail=(
            f"resolved to {len(ordered)} address"
            f"{'es' if len(ordered) != 1 else ''} in {elapsed:g} ms"
        ),
        elapsed_ms=elapsed,
        data={
            "addresses": ordered,
            "ipv4": list(dict.fromkeys(ipv4)),
            "ipv6": list(dict.fromkeys(ipv6)),
        },
    )


def _is_ip_literal(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------- TCP stage
async def _tcp_one(address: str, port: int) -> dict[str, Any]:
    started = perf_counter()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host=address, port=port), timeout=PROBE_TIMEOUT
        )
        return {
            "address": address,
            "status": OK,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    except asyncio.TimeoutError:
        return {
            "address": address, "status": FAILED, "error": "timed out",
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    except ConnectionRefusedError:
        return {
            "address": address, "status": FAILED, "error": "connection refused",
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    except OSError as exc:
        return {
            "address": address, "status": FAILED, "error": str(exc),
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass


async def _tcp_stage(addresses: list[str], port: int) -> Layer:
    """Connect to each address separately.

    A load-balanced hostname can have one dead backend while the others serve
    happily - that shows up here as a partial failure and nowhere else.
    """
    if not addresses:
        return Layer(layer="tcp", status=SKIPPED, detail="no addresses to try")

    targets = addresses[:MAX_ADDRESSES]
    results = await asyncio.gather(*(_tcp_one(a, port) for a in targets))
    ok = [r for r in results if r["status"] == OK]
    bad = [r for r in results if r["status"] != OK]

    if ok and not bad:
        fastest = min(r["elapsed_ms"] for r in ok)
        status, detail = OK, (
            f"all {len(ok)} address(es) accepted a connection on port {port} "
            f"(fastest {fastest:g} ms)"
        )
    elif ok and bad:
        status, detail = WARNING, (
            f"{len(ok)} of {len(targets)} addresses accepted a connection; "
            f"{', '.join(r['address'] + ' ' + r.get('error', 'failed') for r in bad)}"
        )
    else:
        status, detail = FAILED, (
            f"no address accepted a connection on port {port} - "
            f"{bad[0].get('error', 'failed')}"
        )

    return Layer(
        layer="tcp",
        status=status,
        detail=detail,
        elapsed_ms=min((r["elapsed_ms"] for r in ok), default=None),
        data={"attempts": results, "reachable": len(ok), "total": len(targets)},
    )


# --------------------------------------------------------------- TLS stage
async def _tls_stage(endpoint: Endpoint, address: str | None) -> Layer:
    if endpoint.protocol != "https":
        return Layer(
            layer="tls", status=SKIPPED,
            detail=f"{endpoint.protocol}:// endpoint - no TLS layer",
        )

    started = perf_counter()
    verified = await probe_tls(
        endpoint.hostname, endpoint.port,
        timeout=PROBE_TIMEOUT, warning_days=30, critical_days=7,
        verify=True, resolved_ip=address,
    )
    elapsed = round((perf_counter() - started) * 1000, 1)

    data: dict[str, Any] = {
        "tls_version": verified.tls_version,
        "cipher": verified.tls_cipher,
        "common_name": verified.common_name,
        "issuer": verified.issuer_common_name or verified.issuer,
        "days_remaining": verified.days_remaining,
        "expires_at": verified.valid_to.isoformat() if verified.valid_to else None,
        "self_signed": verified.is_self_signed,
        "hostname_matches": verified.hostname_matches,
        "chain_verified": verified.chain_verified,
        "verification_status": verified.verification_status,
        "chain_length": verified.chain_length,
        "san_count": len(verified.san or []),
    }

    if verified.chain_verified and not verified.error:
        return Layer(
            layer="tls", status=OK, elapsed_ms=elapsed,
            detail=(
                f"{verified.tls_version or 'TLS'} handshake succeeded, "
                f"certificate valid for {verified.days_remaining} more day(s)"
            ),
            data=data,
        )

    # Verification failed. Retry without it to separate "the certificate is
    # not trusted" from "nothing is listening" - completely different fixes.
    unverified = await probe_tls(
        endpoint.hostname, endpoint.port,
        timeout=PROBE_TIMEOUT, warning_days=30, critical_days=7,
        verify=False, resolved_ip=address,
    )
    data["verification_error"] = verified.verification_error or verified.error
    if unverified.fingerprint_sha256:
        data.update({
            "unverified_handshake": True,
            "common_name": unverified.common_name,
            "issuer": unverified.issuer_common_name or unverified.issuer,
            "days_remaining": unverified.days_remaining,
            "expires_at": unverified.valid_to.isoformat() if unverified.valid_to else None,
            "self_signed": unverified.is_self_signed,
            "hostname_matches": unverified.hostname_matches,
            "tls_version": unverified.tls_version,
        })
        return Layer(
            layer="tls", status=FAILED, elapsed_ms=elapsed,
            detail=(
                "handshake completes but the certificate is not trusted: "
                f"{data['verification_error']}"
            ),
            data=data,
        )

    return Layer(
        layer="tls", status=FAILED, elapsed_ms=elapsed,
        detail=f"TLS handshake failed: {verified.error or data['verification_error']}",
        data=data,
    )


# -------------------------------------------------------------- HTTP stage
async def _http_probe(
    url: str, *, method: str, headers: dict[str, str], verify: bool,
    follow_redirects: bool,
) -> dict[str, Any]:
    client = transport_module.build_async_client(
        verify=verify, timeout=PROBE_TIMEOUT, follow_redirects=follow_redirects
    )
    started = perf_counter()
    try:
        response = await client.request(method, url, headers=headers)
        return {
            "status": OK,
            "http_status": response.status_code,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
            "final_url": str(response.url),
            "redirects": len(response.history),
            "location": response.headers.get("location"),
            "server": response.headers.get("server"),
            "content_type": response.headers.get("content-type"),
            "content_length": len(response.content) if response.content else 0,
        }
    except Exception as exc:
        return {
            "status": FAILED,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    finally:
        await client.aclose()


async def _http_stage(endpoint: Endpoint, secret: str | None) -> tuple[Layer, dict]:
    """Probe the configured URL, and - when it fails - probe the site root.

    A 404 on ``/health`` while ``/`` answers 200 is the single most common
    false alarm in endpoint monitoring: the service is fine and the monitor is
    pointed at the wrong path.
    """
    if endpoint.check_type != "http":
        return (
            Layer(
                layer="http", status=SKIPPED,
                detail=f"check type is '{endpoint.check_type}' - no HTTP request is made",
            ),
            {},
        )

    target = build_target_from_endpoint(endpoint, auth_secret=secret)
    headers = build_headers(target)
    expected = endpoint.expected_status_list

    primary = await _http_probe(
        endpoint.url, method=endpoint.http_method, headers=headers,
        verify=endpoint.verify_ssl, follow_redirects=endpoint.follow_redirects,
    )

    extras: dict[str, Any] = {}
    if primary["status"] == OK and primary["http_status"] in expected:
        hops = primary["redirects"]
        detail = f"HTTP {primary['http_status']} in {primary['elapsed_ms']:g} ms"
        if hops:
            detail += f" after {hops} redirect(s)"
        return (
            Layer(
                layer="http", status=OK, elapsed_ms=primary["elapsed_ms"],
                detail=detail, data=primary,
            ),
            extras,
        )

    # Failed - gather comparisons that narrow the cause.
    root = f"{endpoint.protocol}://{endpoint.hostname}:{endpoint.port}/"
    if (endpoint.path or "/") not in ("/", ""):
        extras["root"] = await _http_probe(
            root, method="GET", headers=headers,
            verify=endpoint.verify_ssl, follow_redirects=True,
        )
    if endpoint.verify_ssl and primary["status"] == FAILED:
        extras["insecure"] = await _http_probe(
            endpoint.url, method=endpoint.http_method, headers=headers,
            verify=False, follow_redirects=endpoint.follow_redirects,
        )

    if primary["status"] == FAILED:
        detail = f"request failed: {primary['error']}"
    else:
        detail = (
            f"HTTP {primary['http_status']}, which is not in the expected list "
            f"({endpoint.expected_status_codes})"
        )
    return (
        Layer(
            layer="http", status=FAILED, elapsed_ms=primary["elapsed_ms"],
            detail=detail, data=primary,
        ),
        extras,
    )


# ------------------------------------------------------------- history
async def _history(session: AsyncSession, endpoint: Endpoint) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=HISTORY_WINDOW_HOURS)
    rows = (
        await session.execute(
            select(
                MonitoringResult.status,
                MonitoringResult.failure_reason,
                MonitoringResult.http_status_code,
                MonitoringResult.response_time_ms,
                MonitoringResult.checked_at,
                MonitoringResult.resolved_ip,
            )
            .where(
                MonitoringResult.endpoint_id == endpoint.id,
                MonitoringResult.checked_at >= since,
            )
            .order_by(MonitoringResult.checked_at.desc())
            .limit(HISTORY_SAMPLE)
        )
    ).all()

    reasons = Counter()
    codes = Counter()
    ips = Counter()
    transitions = 0
    previous: str | None = None
    latencies: list[float] = []

    # Rows are newest-first; walk oldest-first to count state changes.
    for status, reason, code, latency, _at, ip in reversed(rows):
        if status == CheckStatus.DOWN.value:
            reasons[reason or "unknown"] += 1
        if code is not None:
            codes[str(code)] += 1
        if ip:
            ips[ip] += 1
        if latency is not None:
            latencies.append(float(latency))
        if previous is not None and status != previous:
            transitions += 1
        previous = status

    ever = (
        await session.execute(
            select(func.count(MonitoringResult.id)).where(
                MonitoringResult.endpoint_id == endpoint.id,
                MonitoringResult.status != CheckStatus.DOWN.value,
            )
        )
    ).scalar() or 0

    last_ok = (
        await session.execute(
            select(func.max(MonitoringResult.checked_at)).where(
                MonitoringResult.endpoint_id == endpoint.id,
                MonitoringResult.status != CheckStatus.DOWN.value,
            )
        )
    ).scalar()

    total_checks = (
        await session.execute(
            select(func.count(MonitoringResult.id)).where(
                MonitoringResult.endpoint_id == endpoint.id
            )
        )
    ).scalar() or 0

    return {
        "window_hours": HISTORY_WINDOW_HOURS,
        "checks_analysed": len(rows),
        "total_checks_recorded": int(total_checks),
        "failure_reasons": dict(reasons.most_common()),
        "http_status_codes": dict(codes.most_common()),
        "resolved_ips": dict(ips.most_common()),
        "state_transitions": transitions,
        "ever_succeeded": int(ever) > 0,
        "last_success_at": last_ok,
        "avg_response_time_ms": (
            round(sum(latencies) / len(latencies), 1) if latencies else None
        ),
    }


# --------------------------------------------------------- correlation
async def _correlation(session: AsyncSession, endpoint: Endpoint) -> dict[str, Any]:
    """Is this one service broken, or is something broader wrong?"""
    same_host = (
        await session.execute(
            select(Endpoint.name, Endpoint.current_status, Endpoint.url)
            .where(
                Endpoint.hostname == endpoint.hostname,
                Endpoint.id != endpoint.id,
            )
            .limit(25)
        )
    ).all()

    env_rows = (
        await session.execute(
            select(Endpoint.current_status, func.count(Endpoint.id))
            .where(
                Endpoint.environment_id == endpoint.environment_id,
                Endpoint.monitoring_enabled.is_(True),
                Endpoint.is_paused.is_(False),
            )
            .group_by(Endpoint.current_status)
        )
    ).all()
    env_counts = {str(s): int(c) for s, c in env_rows}
    env_total = sum(env_counts.values())
    env_down = env_counts.get(EndpointStatus.DOWN.value, 0)

    all_rows = (
        await session.execute(
            select(Endpoint.current_status, func.count(Endpoint.id))
            .where(
                Endpoint.monitoring_enabled.is_(True),
                Endpoint.is_paused.is_(False),
            )
            .group_by(Endpoint.current_status)
        )
    ).all()
    all_counts = {str(s): int(c) for s, c in all_rows}
    fleet_total = sum(all_counts.values())
    fleet_down = all_counts.get(EndpointStatus.DOWN.value, 0)

    return {
        "same_hostname": [
            {"name": n, "status": s, "url": u} for n, s, u in same_host
        ],
        "same_hostname_down": sum(
            1 for _n, s, _u in same_host if s == EndpointStatus.DOWN.value
        ),
        "environment_total": env_total,
        "environment_down": env_down,
        "fleet_total": fleet_total,
        "fleet_down": fleet_down,
    }


# ------------------------------------------------------------- verdict
def _analyse(
    endpoint: Endpoint,
    layers: dict[str, Layer],
    extras: dict[str, Any],
    history: dict[str, Any],
    correlation: dict[str, Any],
) -> tuple[str, str, list[Finding]]:
    findings: list[Finding] = []
    dns, tcp, tls, http = (layers.get(k) for k in ("dns", "tcp", "tls", "http"))

    # ---- fleet-wide correlation first: it reframes everything below.
    fleet_total = correlation["fleet_total"]
    fleet_down = correlation["fleet_down"]
    if fleet_total >= 5 and fleet_down >= max(3, int(fleet_total * 0.6)):
        findings.append(Finding(
            severity="high",
            title=f"{fleet_down} of {fleet_total} monitored endpoints are down",
            detail=(
                "When most of the fleet fails at once, the common cause is "
                "usually the monitoring host's own network - blocked egress, "
                "DNS, or a security group change - rather than every service "
                "failing independently."
            ),
            action=(
                "Check outbound connectivity from the worker: "
                "docker compose exec worker python -c "
                "\"import socket; print(socket.getaddrinfo('example.com', 443))\""
            ),
        ))

    # ---- DNS
    if dns and dns.status == FAILED:
        findings.append(Finding(
            severity="high",
            title="DNS resolution failed",
            detail=f"{endpoint.hostname} could not be resolved. {dns.detail}",
            action=(
                "Confirm the record exists and is public. If it is internal-only, "
                "the worker container needs a resolver that can see it - add a "
                "`dns:` entry to the worker service in docker-compose.yml."
            ),
        ))
        return ("dns_failure",
                f"Nothing beyond DNS could be tested: {endpoint.hostname} does not resolve.",
                findings)

    addresses = (dns.data.get("addresses") if dns else []) or []
    known_ips = set(history.get("resolved_ips") or {})
    if addresses and known_ips and not known_ips & set(addresses):
        findings.append(Finding(
            severity="medium",
            title="The resolved address changed",
            detail=(
                f"Checks previously reached {', '.join(sorted(known_ips))} but DNS "
                f"now returns {', '.join(addresses)}. A cutover or failover would "
                "explain a sudden failure."
            ),
            action="Confirm the new address is the intended target and is serving.",
        ))

    # ---- TCP
    if tcp and tcp.status == FAILED:
        refused = any(
            "refused" in (a.get("error") or "") for a in tcp.data.get("attempts", [])
        )
        findings.append(Finding(
            severity="high",
            title="No address accepted a TCP connection",
            detail=(
                f"DNS resolves, but port {endpoint.port} is unreachable on every "
                f"address. {tcp.detail}"
            ),
            action=(
                "The service is not listening, or a firewall/security group is "
                "dropping the connection."
                if refused else
                "A silent timeout usually means a firewall or security group is "
                "dropping packets rather than rejecting them."
            ),
        ))
        return ("connection_failed",
                f"The host resolves but nothing is accepting connections on port {endpoint.port}.",
                findings)

    if tcp and tcp.status == WARNING:
        findings.append(Finding(
            severity="high",
            title="Some backend addresses are unreachable",
            detail=(
                f"{tcp.detail}. Requests balanced onto the failing address will "
                "fail intermittently while others succeed - which is what "
                "flapping usually is."
            ),
            action=(
                "Check the load balancer target group / upstream pool and remove "
                "or repair the unhealthy member."
            ),
        ))

    # ---- TLS
    if tls and tls.status == FAILED:
        d = tls.data
        if d.get("days_remaining") is not None and d["days_remaining"] < 0:
            findings.append(Finding(
                severity="high",
                title="The TLS certificate has expired",
                detail=(
                    f"It expired {abs(d['days_remaining'])} day(s) ago "
                    f"({d.get('expires_at')}). Clients that verify certificates "
                    "will refuse to connect."
                ),
                action="Renew and deploy the certificate, then re-check.",
            ))
        elif d.get("hostname_matches") is False:
            findings.append(Finding(
                severity="high",
                title="The certificate does not cover this hostname",
                detail=(
                    f"It is issued for {d.get('common_name')} but was requested as "
                    f"{endpoint.hostname}."
                ),
                action=(
                    "Add the hostname to the certificate's SAN list, or point the "
                    "endpoint at the name the certificate covers."
                ),
            ))
        elif d.get("self_signed"):
            findings.append(Finding(
                severity="medium",
                title="The certificate is self-signed",
                detail="No public CA vouches for it, so chain verification fails.",
                action=(
                    "For an internal CA this is expected - turn off "
                    "\"Verify the certificate chain\" for this endpoint. Expiry is "
                    "still tracked."
                ),
            ))
        elif d.get("unverified_handshake"):
            findings.append(Finding(
                severity="high",
                title="The certificate chain is incomplete or untrusted",
                detail=(
                    f"The handshake completes, so the service is up, but "
                    f"verification fails: {d.get('verification_error')}. A missing "
                    "intermediate certificate is the usual cause - browsers often "
                    "paper over it, strict clients do not."
                ),
                action=(
                    "Serve the full chain (leaf + intermediates). Verify with: "
                    f"openssl s_client -connect {endpoint.hostname}:{endpoint.port} "
                    f"-servername {endpoint.hostname} -showcerts"
                ),
            ))
        else:
            findings.append(Finding(
                severity="high",
                title="TLS handshake failed",
                detail=tls.detail,
                action=(
                    "Check the TLS version and cipher the server offers. A very old "
                    "or misconfigured terminator can refuse a modern client."
                ),
            ))
        return ("tls_failure", f"TLS is the failing layer: {tls.detail}", findings)

    if tls and tls.status == OK:
        days = tls.data.get("days_remaining")
        if days is not None and days <= 14:
            findings.append(Finding(
                severity="high" if days <= 7 else "medium",
                title=f"The certificate expires in {days} day(s)",
                detail=f"Issued by {tls.data.get('issuer')}, expires {tls.data.get('expires_at')}.",
                action="Renew it now - this is unrelated to the current failure but will cause one.",
            ))

    # ---- HTTP
    if http and http.status == FAILED:
        code = http.data.get("http_status")
        root = extras.get("root")
        insecure = extras.get("insecure")

        if insecure and insecure.get("status") == OK:
            findings.append(Finding(
                severity="high",
                title="The request only succeeds with verification disabled",
                detail=(
                    f"With chain verification off the endpoint answers HTTP "
                    f"{insecure.get('http_status')}. The service is up; the "
                    "certificate is the problem."
                ),
                action="Fix the certificate chain, or disable verification for this endpoint.",
            ))

        if code is None:
            findings.append(Finding(
                severity="high",
                title="The HTTP request did not complete",
                detail=http.detail,
                action=(
                    "TCP and TLS succeeded, so the listener is alive but the "
                    "application is not answering. Check application logs and "
                    "whether it is stuck rather than down."
                ),
            ))
            return ("http_no_response",
                    "The transport is healthy but the application never answered.",
                    findings)

        if code in (502, 503, 504):
            findings.append(Finding(
                severity="high",
                title=f"The proxy returned HTTP {code}",
                detail=(
                    f"{code} comes from the reverse proxy or load balancer, not the "
                    "application - it means the upstream is unreachable, "
                    "overloaded, or timing out. The edge is healthy; what sits "
                    "behind it is not."
                ),
                action=(
                    "Check the application container/pod and the proxy's upstream "
                    "health. Look at the proxy's own error log for the upstream address."
                ),
            ))
            return (f"upstream_unavailable_{code}",
                    f"The edge is serving but its upstream is failing (HTTP {code}).",
                    findings)

        if code == 404:
            if root and root.get("status") == OK and root.get("http_status") not in (404,):
                findings.append(Finding(
                    severity="high",
                    title="The path is wrong, not the service",
                    detail=(
                        f"{endpoint.path} returns 404 while / returns "
                        f"{root.get('http_status')}. The host is serving normally; "
                        "the monitored path does not exist."
                    ),
                    action=(
                        "Point the endpoint at a real health path (/health, "
                        "/actuator/health, /api/health) and expect 200."
                    ),
                ))
                return ("wrong_path",
                        "The service is up - the monitored path returns 404.",
                        findings)
            findings.append(Finding(
                severity="medium",
                title="HTTP 404 at the monitored URL",
                detail=(
                    "The server is answering, so it is up, but nothing is routed at "
                    "this path. Many APIs legitimately 404 at their root."
                ),
                action=(
                    "Either monitor a real health path, or add 404 to this "
                    "endpoint's expected status codes if 404 is correct here."
                ),
            ))
            return ("likely_wrong_path_or_expectation",
                    "The host answers but returns 404 at the monitored path.",
                    findings)

        if code in (401, 403):
            findings.append(Finding(
                severity="medium",
                title=f"The endpoint requires authentication (HTTP {code})",
                detail=(
                    "The service is up and rejecting an unauthenticated request. "
                    "That is a configuration mismatch, not an outage."
                ),
                action=(
                    "Add credentials under the endpoint's authentication settings "
                    f"(stored encrypted), or add {code} to the expected status codes "
                    "if an auth challenge is the correct healthy response."
                ),
            ))
            return ("auth_required",
                    f"The service is up but requires authentication (HTTP {code}).",
                    findings)

        if 500 <= code < 600:
            findings.append(Finding(
                severity="high",
                title=f"The application returned HTTP {code}",
                detail=(
                    "The request reached the application and it failed internally. "
                    "This is an application fault, not infrastructure."
                ),
                action="Check the application logs for this request; correlate on timestamp.",
            ))
            return ("application_error", f"The application is failing with HTTP {code}.", findings)

        if 300 <= code < 400:
            findings.append(Finding(
                severity="low",
                title=f"The endpoint redirects (HTTP {code})",
                detail=f"Location: {http.data.get('location') or 'not sent'}.",
                action=(
                    "Enable \"Follow redirects\", monitor the final URL directly, or "
                    f"add {code} to the expected status codes."
                ),
            ))
            return ("unexpected_redirect", f"The endpoint redirects with HTTP {code}.", findings)

        findings.append(Finding(
            severity="medium",
            title=f"Unexpected HTTP {code}",
            detail=f"Expected one of {endpoint.expected_status_codes}.",
            action="Either fix the service or update the expected status codes.",
        ))
        return ("http_status_mismatch",
                f"The service answers with HTTP {code}, which is not expected.",
                findings)

    # ---- everything probed clean
    if not history.get("ever_succeeded") and history.get("total_checks_recorded"):
        findings.append(Finding(
            severity="high",
            title="This endpoint has never passed a check",
            detail=(
                "Every recorded check has failed. A monitor that was wrong from "
                "the start is far more often a configuration error than a service "
                "that has been down since it was added."
            ),
            action="Re-read the URL, port, expected status and authentication settings.",
        ))

    transitions = history.get("state_transitions", 0)
    if transitions >= 6:
        findings.append(Finding(
            severity="medium",
            title=f"The endpoint is flapping ({transitions} state changes in "
                  f"{history['window_hours']}h)",
            detail=(
                "Alternating up and down usually means one unhealthy member behind "
                "a load balancer, or a timeout set close to the real response time."
            ),
            action=(
                "Compare the timeout with the observed response time, and check "
                "each backend individually."
            ),
        ))

    if endpoint.current_status == EndpointStatus.DOWN.value:
        return ("recovered_since_last_check",
                "Every layer responds correctly now. The endpoint appears to have "
                "recovered since its last scheduled check.",
                findings)

    return ("healthy", "All layers responded correctly.", findings)


# ------------------------------------------------------------- entrypoint
async def diagnose(session: AsyncSession, endpoint: Endpoint) -> dict[str, Any]:
    """Run a full triage of one endpoint. Writes nothing to the history."""
    started = perf_counter()

    secret = (
        decrypt_secret(endpoint.auth_secret_encrypted)
        if endpoint.auth_secret_encrypted
        else None
    )

    dns = await _dns_stage(endpoint.hostname, endpoint.port)
    addresses = dns.data.get("addresses", []) if dns.status in (OK, SKIPPED) else []

    blocked_note = None
    if addresses:
        blocked, why = is_blocked_address(addresses[0])
        if blocked:
            blocked_note = why

    tcp = (
        await _tcp_stage(addresses, endpoint.port)
        if addresses and not blocked_note
        else Layer(
            layer="tcp", status=SKIPPED,
            detail=blocked_note or "no addresses to try",
        )
    )

    first_ok = next(
        (a["address"] for a in tcp.data.get("attempts", []) if a["status"] == OK),
        addresses[0] if addresses else None,
    )

    tls = (
        await _tls_stage(endpoint, first_ok)
        if tcp.status in (OK, WARNING)
        else Layer(layer="tls", status=SKIPPED, detail="TCP did not connect")
    )

    if tcp.status in (OK, WARNING) and tls.status in (OK, SKIPPED):
        http, extras = await _http_stage(endpoint, secret)
    else:
        http, extras = (
            Layer(
                layer="http", status=SKIPPED,
                detail="a lower layer failed - fix that first",
            ),
            {},
        )

    layers = {"dns": dns, "tcp": tcp, "tls": tls, "http": http}
    history = await _history(session, endpoint)
    correlation = await _correlation(session, endpoint)
    verdict, summary, findings = _analyse(
        endpoint, layers, extras, history, correlation
    )

    if blocked_note:
        findings.insert(0, Finding(
            severity="high",
            title="This target is refused by policy",
            detail=(
                f"{endpoint.hostname} resolves to {addresses[0]}, which is "
                f"not probed: {blocked_note}."
            ),
            action=(
                "Point the endpoint at a routable address, or set "
                "ALLOW_LOOPBACK_TARGETS=true if you really mean to check the "
                "monitoring host itself."
            ),
        ))

    # The deepest layer that worked - the single most useful line in a triage.
    order = ["dns", "tcp", "tls", "http"]
    deepest = None
    for name in order:
        if layers[name].status in (OK, WARNING):
            deepest = name
        elif layers[name].status == FAILED:
            break

    elapsed = round((perf_counter() - started) * 1000, 1)
    logger.info(
        "endpoint_diagnosed",
        endpoint=endpoint.name,
        verdict=verdict,
        deepest_layer_ok=deepest,
        elapsed_ms=elapsed,
    )

    return {
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "url": endpoint.url,
        "current_status": endpoint.current_status,
        "generated_at": datetime.now(timezone.utc),
        "elapsed_ms": elapsed,
        "verdict": verdict,
        "summary": summary,
        "deepest_layer_ok": deepest,
        "layers": [
            {
                "layer": layer.layer,
                "status": layer.status,
                "detail": layer.detail,
                "elapsed_ms": layer.elapsed_ms,
                "data": layer.data,
            }
            for layer in (dns, tcp, tls, http)
        ],
        "findings": [
            {
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "action": f.action,
            }
            for f in findings
        ],
        "comparisons": extras,
        "history": history,
        "correlation": correlation,
    }
