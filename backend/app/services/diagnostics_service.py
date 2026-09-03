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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ActionRisk,
    ChangeStatus,
    CheckStatus,
    DiagnosisFocus,
    EndpointStatus,
    EvidenceKind,
    IncidentStatus,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.models.endpoint import Endpoint
from app.models.monitoring import MonitoringResult
from app.monitoring import transport as transport_module
from app.monitoring.checker import build_headers, build_target_from_endpoint
from app.monitoring.ssl_inspect import probe_tls
from app.monitoring.validators import is_blocked_address
from app.services.diagnosis_reasoning import (
    CIRCUMSTANTIAL,
    DIRECT,
    STRONG,
    SUPPORTING,
    Action,
    Candidate,
    Evidence,
    blind_spots,
    diagnostic_commands,
    is_total_application_outage,
    rank,
    severity_for,
    verification_plan,
)

logger = get_logger(__name__)

# Diagnostics runs several probes back to back, so each one is kept short -
# an operator is waiting on the response.
PROBE_TIMEOUT = 8.0
# Cap the per-address TCP fan-out: enough to spot one bad backend, bounded so
# a host with 20 A records cannot stall the request.
MAX_ADDRESSES = 4
HISTORY_WINDOW_HOURS = 24
HISTORY_SAMPLE = 200

# How far back to look for a deployment that could explain the failure, and
# how close it has to be to count as a correlation. Ninety minutes is
# generous on purpose: a bad release often degrades slowly rather than
# failing the instant it lands.
CHANGE_LOOKBACK_HOURS = 24
CHANGE_CORRELATION_MINUTES = 90

# Window for "has this happened before" - long enough to expose a weekly
# pattern, short enough that a fix from last quarter does not muddy it.
RECURRENCE_WINDOW_DAYS = 30

# How many recent checks the availability strip shows.
RECENT_STRIP_SIZE = 30
# A response this many times its own baseline is treated as degradation
# rather than noise.
LATENCY_ANOMALY_RATIO = 2.0

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

    # ---- availability over the recent window (rows are newest-first)
    strip = [
        {
            "status": status,
            "at": checked_at,
            "code": code,
            "ms": round(float(latency), 1) if latency is not None else None,
        }
        for status, _reason, code, latency, checked_at, _ip in rows[
            :RECENT_STRIP_SIZE
        ]
    ][::-1]
    passed = sum(1 for item in strip if item["status"] != CheckStatus.DOWN.value)
    availability = round(passed / len(strip) * 100, 1) if strip else None

    # ---- latency baseline vs now
    #
    # The baseline deliberately excludes the most recent few checks: if the
    # endpoint is slow *right now*, including the current slowness in its own
    # baseline would hide the very anomaly being looked for.
    baseline_pool = [
        float(latency)
        for _s, _r, _c, latency, _at, _ip in rows[5:]
        if latency is not None
    ]
    baseline = (
        round(sorted(baseline_pool)[len(baseline_pool) // 2], 1)
        if len(baseline_pool) >= 5
        else None
    )
    current_pool = [
        float(latency)
        for _s, _r, _c, latency, _at, _ip in rows[:5]
        if latency is not None
    ]
    current = round(sum(current_pool) / len(current_pool), 1) if current_pool else None
    ratio = (
        round(current / baseline, 2)
        if baseline and current and baseline > 0
        else None
    )

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
        "recent_checks": strip,
        "recent_availability_pct": availability,
        "baseline_response_time_ms": baseline,
        "current_response_time_ms": current,
        "latency_ratio": ratio,
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


# ------------------------------------------------- application blast radius
async def _application_summary(
    session: AsyncSession, endpoint: Endpoint
) -> dict[str, Any] | None:
    """How the rest of this application is doing.

    One endpoint down is a fault; every endpoint of an application down is an
    outage. The distinction changes the severity and who needs to know.
    """
    if not endpoint.application:
        return None

    rows = (
        await session.execute(
            select(Endpoint.current_status, func.count(Endpoint.id))
            .where(
                func.lower(Endpoint.application) == endpoint.application.lower(),
                Endpoint.monitoring_enabled.is_(True),
                Endpoint.is_paused.is_(False),
            )
            .group_by(Endpoint.current_status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    total = sum(counts.values())
    return {
        "application": endpoint.application,
        "total": total,
        "down": counts.get(EndpointStatus.DOWN.value, 0),
        "degraded": counts.get(EndpointStatus.DEGRADED.value, 0),
        "up": counts.get(EndpointStatus.UP.value, 0),
        "by_status": counts,
    }


# ------------------------------------------------------ failure onset
async def _failure_onset(
    session: AsyncSession, endpoint: Endpoint
) -> datetime | None:
    """When the current run of failures began.

    Everything time-based downstream hangs off this: correlating a deployment
    against "now" is meaningless, because an endpoint may have been failing
    for six hours before anyone pressed Diagnose.
    """
    rows = (
        await session.execute(
            select(MonitoringResult.status, MonitoringResult.checked_at)
            .where(MonitoringResult.endpoint_id == endpoint.id)
            .order_by(MonitoringResult.checked_at.desc())
            .limit(HISTORY_SAMPLE)
        )
    ).all()

    onset: datetime | None = None
    for status, checked_at in rows:
        if status == CheckStatus.DOWN.value:
            onset = checked_at
        else:
            break
    return onset


# --------------------------------------------------- deployment correlation
async def _change_correlation(
    session: AsyncSession, endpoint: Endpoint, onset: datetime | None
) -> dict[str, Any]:
    """Did a deployment happen just before this started failing?

    Timing is the whole signal here, and timing is correlation, never proof -
    the wording throughout says so. But when a production release finished
    three minutes before an endpoint started returning 502, it is the first
    thing any engineer would want to know.
    """
    from app.models.change import Change, change_endpoints

    now = datetime.now(timezone.utc)
    reference = onset or now

    active = (
        await session.execute(
            select(Change)
            .join(change_endpoints, change_endpoints.c.change_id == Change.id)
            .where(
                change_endpoints.c.endpoint_id == endpoint.id,
                Change.status == ChangeStatus.DEPLOYMENT_IN_PROGRESS.value,
            )
            .limit(1)
        )
    ).scalars().first()

    # Anything that finished in the window before the failure started, either
    # linked to this endpoint directly or covering the same application.
    window_start = reference - timedelta(hours=CHANGE_LOOKBACK_HOURS)
    stmt = (
        select(Change)
        .where(
            Change.completed_at.is_not(None),
            Change.completed_at >= window_start,
            Change.completed_at <= reference + timedelta(minutes=5),
        )
        .order_by(Change.completed_at.desc())
        .limit(10)
    )
    if endpoint.application:
        stmt = stmt.where(
            or_(
                func.lower(Change.application) == endpoint.application.lower(),
                Change.id.in_(
                    select(change_endpoints.c.change_id).where(
                        change_endpoints.c.endpoint_id == endpoint.id
                    )
                ),
            )
        )
    else:
        stmt = stmt.where(
            Change.id.in_(
                select(change_endpoints.c.change_id).where(
                    change_endpoints.c.endpoint_id == endpoint.id
                )
            )
        )

    recent = (await session.execute(stmt)).scalars().unique().all()

    def describe(change: Any, *, gap_from: datetime | None) -> dict[str, Any]:
        gap_minutes = None
        if gap_from and change.completed_at:
            gap_minutes = round(
                (gap_from - change.completed_at).total_seconds() / 60, 1
            )
        return {
            "id": change.id,
            "reference": change.reference,
            "title": change.title,
            "application": change.application,
            "environment": change.environment_name,
            "status": change.status,
            "risk": change.risk,
            "deployer_name": change.deployer_name,
            "started_at": change.started_at,
            "completed_at": change.completed_at,
            "minutes_before_failure": gap_minutes,
        }

    described = [describe(c, gap_from=onset) for c in recent]

    # The strongest correlation is the deployment that finished most recently
    # before the failure began - and only if it did finish before it.
    closest = None
    for item in described:
        gap = item["minutes_before_failure"]
        if gap is not None and 0 <= gap <= CHANGE_CORRELATION_MINUTES:
            closest = item
            break

    return {
        "active_deployment": describe(active, gap_from=None) if active else None,
        "recent": described,
        "closest": closest,
        "correlation_window_minutes": CHANGE_CORRELATION_MINUTES,
        "failure_started_at": onset,
    }


# ----------------------------------------------------- incident correlation
async def _incident_correlation(
    session: AsyncSession, endpoint: Endpoint
) -> dict[str, Any]:
    """The open incident, if any, plus how often this has happened lately."""
    from app.models.incident import Incident

    open_incident = (
        await session.execute(
            select(Incident)
            .where(
                Incident.endpoint_id == endpoint.id,
                Incident.status == IncidentStatus.OPEN.value,
            )
            .order_by(Incident.started_at.desc())
            .limit(1)
        )
    ).scalars().first()

    since = datetime.now(timezone.utc) - timedelta(days=RECURRENCE_WINDOW_DAYS)
    past = (
        await session.execute(
            select(Incident.reason, Incident.started_at, Incident.duration_seconds)
            .where(
                Incident.endpoint_id == endpoint.id,
                Incident.started_at >= since,
            )
            .order_by(Incident.started_at.desc())
            .limit(50)
        )
    ).all()

    reasons = Counter(reason or "unknown" for reason, _s, _d in past)
    durations = [d for _r, _s, d in past if d]

    return {
        "open_incident": (
            {
                "id": open_incident.id,
                "started_at": open_incident.started_at,
                "severity": open_incident.severity,
                "reason": open_incident.reason,
                "error_message": open_incident.error_message,
                "failed_check_count": open_incident.failed_check_count,
                "acknowledged_at": open_incident.acknowledged_at,
            }
            if open_incident
            else None
        ),
        "window_days": RECURRENCE_WINDOW_DAYS,
        "incident_count": len(past),
        "most_common_reason": reasons.most_common(1)[0][0] if reasons else None,
        "most_common_reason_count": (
            reasons.most_common(1)[0][1] if reasons else 0
        ),
        "reason_breakdown": dict(reasons.most_common()),
        "median_duration_seconds": (
            sorted(durations)[len(durations) // 2] if durations else None
        ),
    }


# ------------------------------------------------------- past diagnoses
async def _recurrence(session: AsyncSession, endpoint: Endpoint) -> dict[str, Any]:
    """Have we diagnosed this same thing before?

    A verdict seen three times in a month is a pattern, and the resolution an
    operator recorded last time is usually worth more than anything this
    engine can derive from scratch.
    """
    from app.models.diagnosis import Diagnosis

    since = datetime.now(timezone.utc) - timedelta(days=RECURRENCE_WINDOW_DAYS)
    rows = (
        await session.execute(
            select(
                Diagnosis.verdict,
                Diagnosis.headline,
                Diagnosis.created_at,
                Diagnosis.resolution,
            )
            .where(
                Diagnosis.endpoint_id == endpoint.id,
                Diagnosis.created_at >= since,
            )
            .order_by(Diagnosis.created_at.desc())
            .limit(50)
        )
    ).all()

    verdicts = Counter(v for v, _h, _c, _r in rows)
    resolutions = [
        {"at": created, "resolution": resolution, "verdict": verdict}
        for verdict, _h, created, resolution in rows
        if resolution
    ]

    return {
        "window_days": RECURRENCE_WINDOW_DAYS,
        "diagnosis_count": len(rows),
        "verdict_breakdown": dict(verdicts.most_common()),
        "most_common_verdict": verdicts.most_common(1)[0][0] if verdicts else None,
        "most_common_verdict_count": (
            verdicts.most_common(1)[0][1] if verdicts else 0
        ),
        "past_resolutions": resolutions[:5],
        "last_diagnosed_at": rows[0][2] if rows else None,
    }


# ------------------------------------------------------------- verdict
def _http_evidence(layers: dict[str, Layer]) -> tuple[int | None, float | None]:
    http = layers.get("http")
    if not http:
        return None, None
    return http.data.get("http_status"), http.elapsed_ms


def _build_evidence(
    endpoint: Endpoint,
    layers: dict[str, Layer],
    history: dict[str, Any],
    changes: dict[str, Any],
    incidents: dict[str, Any],
    app_summary: dict[str, Any] | None,
) -> list[Evidence]:
    """The observations the conclusion rests on, in the order they were made.

    Rendered as-is in the UI, so an engineer can audit the reasoning instead
    of taking the verdict on trust.
    """
    items: list[Evidence] = []
    labels = {
        "dns": "DNS resolution",
        "tcp": "TCP connectivity",
        "tls": "TLS handshake",
        "http": "HTTP response",
    }
    for name in ("dns", "tcp", "tls", "http"):
        layer = layers.get(name)
        if not layer:
            continue
        items.append(
            Evidence(
                label=labels[name],
                value={
                    OK: "Passed",
                    WARNING: "Partial",
                    FAILED: "Failed",
                    SKIPPED: "Not reached",
                }.get(layer.status, layer.status),
                status=layer.status,
                detail=layer.detail,
            )
        )

    baseline = history.get("baseline_response_time_ms")
    current = history.get("current_response_time_ms")
    ratio = history.get("latency_ratio")
    if baseline and current:
        if ratio and ratio >= LATENCY_ANOMALY_RATIO:
            items.append(
                Evidence(
                    label="Response time",
                    value=f"{current:.0f} ms vs {baseline:.0f} ms baseline",
                    status=WARNING,
                    detail=(
                        f"{ratio:.1f}x its own 24-hour median. Sustained "
                        "slowdown often precedes an outright failure."
                    ),
                )
            )
        else:
            items.append(
                Evidence(
                    label="Response time",
                    value=f"{current:.0f} ms (baseline {baseline:.0f} ms)",
                    status=OK,
                )
            )

    availability = history.get("recent_availability_pct")
    if availability is not None:
        items.append(
            Evidence(
                label=f"Recent availability (last {len(history.get('recent_checks') or [])} checks)",
                value=f"{availability}%",
                status=OK if availability >= 99 else WARNING if availability >= 95 else FAILED,
            )
        )

    closest = changes.get("closest")
    if closest:
        items.append(
            Evidence(
                label="Recent deployment",
                value=(
                    f"{closest['reference']} completed "
                    f"{closest['minutes_before_failure']:.0f} min before the failure"
                ),
                kind=EvidenceKind.OBSERVED.value,
                status=WARNING,
                detail=(
                    f"{closest['application']}"
                    + (f" / {closest['environment']}" if closest.get("environment") else "")
                    + f", deployed by {closest.get('deployer_name') or 'unknown'}. "
                    "Timing is a correlation, not proof of cause."
                ),
            )
        )

    open_incident = incidents.get("open_incident")
    if open_incident:
        items.append(
            Evidence(
                label="Open incident",
                value=f"INC-{open_incident['id']}",
                detail=(
                    f"Opened {open_incident['started_at']}, "
                    f"{open_incident['failed_check_count']} failed check(s). "
                    + (
                        "Acknowledged."
                        if open_incident.get("acknowledged_at")
                        else "Not yet acknowledged."
                    )
                ),
                status=WARNING,
            )
        )

    if app_summary and app_summary["total"] > 1:
        items.append(
            Evidence(
                label=f"Other endpoints of {app_summary['application']}",
                value=f"{app_summary['down']} of {app_summary['total']} down",
                status=FAILED if app_summary["down"] >= app_summary["total"] else OK,
                detail=(
                    "Every endpoint of this application is failing, which "
                    "points at the application or its host rather than this "
                    "one route."
                    if app_summary["down"] >= app_summary["total"]
                    else "The rest of the application is answering normally."
                ),
            )
        )

    return items


def _analyse(
    endpoint: Endpoint,
    layers: dict[str, Layer],
    extras: dict[str, Any],
    history: dict[str, Any],
    correlation: dict[str, Any],
    changes: dict[str, Any],
    incidents: dict[str, Any],
    app_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Turn evidence into a ranked diagnosis.

    Structure: walk the layers outside-in, and at each one both (a) settle the
    verdict if that layer is definitively broken, and (b) score every cause
    the evidence touches. Candidates accumulate across the whole function, so
    a 502 with a deployment three minutes earlier ends up with two scored
    explanations rather than one asserted one.
    """
    dns, tcp, tls, http = (layers.get(k) for k in ("dns", "tcp", "tls", "http"))
    findings: list[Finding] = []
    actions: list[Action] = []
    candidates: dict[str, Candidate] = {}

    def cand(cause: str, label: str, explanation: str) -> Candidate:
        if cause not in candidates:
            candidates[cause] = Candidate(cause=cause, label=label, explanation=explanation)
        return candidates[cause]

    closest_change = changes.get("closest")
    active_deployment = changes.get("active_deployment")
    code, _http_ms = _http_evidence(layers)

    # ------------------------------------------------------------------
    # 0. A deployment is running right now. Nothing below is a fault.
    # ------------------------------------------------------------------
    if active_deployment:
        cand(
            "deployment_in_progress",
            "A deployment is in progress",
            "Monitoring for this endpoint is paused deliberately.",
        ).add(DIRECT, f"{active_deployment['reference']} is deploying now")
        findings.append(Finding(
            severity="low",
            title=f"Deployment {active_deployment['reference']} is in progress",
            detail=(
                f"{active_deployment['application']} is being deployed by "
                f"{active_deployment.get('deployer_name') or 'someone'}, started "
                f"{active_deployment.get('started_at')}. Monitoring for this "
                "endpoint is paused, so its current state is expected and no "
                "incident will be raised."
            ),
            action=(
                "Wait for the deployment to complete. CertMonitor re-checks the "
                "endpoint automatically the moment it does."
            ),
        ))
        ranked, confidence = rank(list(candidates.values()))
        return {
            "verdict": "deployment_in_progress",
            "summary": (
                f"A deployment ({active_deployment['reference']}) is in progress "
                "and monitoring is paused. There is nothing to diagnose yet."
            ),
            "root_cause": None,
            "candidates": ranked,
            "confidence": confidence,
            "findings": findings,
            "actions": [
                Action(
                    title="Wait for the deployment to finish",
                    detail=(
                        "Monitoring resumes automatically and every affected "
                        "endpoint is re-checked immediately, so a broken "
                        "deployment surfaces within seconds."
                    ),
                    risk=ActionRisk.SAFE.value,
                ),
                Action(
                    title="Watch the change record",
                    detail=(
                        f"{active_deployment['reference']} carries the "
                        "deployment notes, the affected endpoints and the "
                        "post-deployment health check."
                    ),
                    risk=ActionRisk.SAFE.value,
                ),
            ],
        }

    # ------------------------------------------------------------------
    # 1. Fleet-wide failure reframes everything else.
    # ------------------------------------------------------------------
    fleet_total = correlation["fleet_total"]
    fleet_down = correlation["fleet_down"]
    fleet_wide = fleet_total >= 5 and fleet_down >= max(3, int(fleet_total * 0.6))
    if fleet_wide:
        cand(
            "monitoring_egress",
            "The monitoring host's own network",
            "Most of the fleet failing at once is rarely many simultaneous "
            "outages; it is usually one thing between CertMonitor and everything else.",
        ).add(STRONG, f"{fleet_down} of {fleet_total} endpoints are down")
        findings.append(Finding(
            severity="high",
            title=f"{fleet_down} of {fleet_total} monitored endpoints are down",
            detail=(
                "When most of the fleet fails together, the common cause is "
                "usually the monitoring host's own egress - DNS, a security "
                "group, or an expired NAT route - rather than every service "
                "failing independently."
            ),
            action=(
                "Check outbound connectivity from the worker before "
                "investigating any single service."
            ),
        ))
        actions.append(Action(
            title="Rule out the monitoring host's own network first",
            detail=(
                "If CertMonitor cannot reach anything, every diagnosis below "
                "is measuring the wrong thing."
            ),
            risk=ActionRisk.SAFE.value,
            command=(
                "docker compose exec worker python -c "
                "\"import socket;print(socket.getaddrinfo('example.com',443))\""
            ),
            command_note="Resolve and connect from inside the worker container.",
        ))

    # ------------------------------------------------------------------
    # 2. DNS
    # ------------------------------------------------------------------
    if dns and dns.status == FAILED:
        cand(
            "dns_failure",
            "DNS does not resolve",
            "The name cannot be turned into an address, so nothing downstream "
            "was reachable to test.",
        ).add(DIRECT, f"{endpoint.hostname} did not resolve")
        if not fleet_wide:
            candidates["dns_failure"].add(
                SUPPORTING, "Other endpoints are resolving normally, so this is name-specific"
            )
        findings.append(Finding(
            severity="high",
            title="DNS resolution failed",
            detail=f"{endpoint.hostname} could not be resolved. {dns.detail}",
            action=(
                "Confirm the record exists. If it is internal-only, the worker "
                "container needs a resolver that can see it."
            ),
        ))
        actions.extend([
            Action(
                title="Confirm the record exists at all",
                detail="Start with public resolution before assuming anything internal.",
                risk=ActionRisk.SAFE.value,
                command=f"dig +short {endpoint.hostname}",
                command_note="Empty output means no A/AAAA record is being served.",
            ),
            Action(
                title="Resolve from inside the worker container",
                detail=(
                    "A name that resolves on your laptop but not in the "
                    "container is a resolver problem, not a DNS record problem."
                ),
                risk=ActionRisk.SAFE.value,
                command=f"docker compose exec worker getent hosts {endpoint.hostname}",
            ),
            Action(
                title="Give the worker a resolver that can see internal names",
                detail=(
                    "If the name is internal-only, add a `dns:` entry to the "
                    "worker service in docker-compose.yml and restart it."
                ),
                risk=ActionRisk.DISRUPTIVE.value,
                command="docker compose up -d worker",
                command_note="Restarts the worker; in-flight checks are re-claimed.",
            ),
        ])
        return _finish_analysis(
            "dns_failure",
            f"Nothing beyond DNS could be tested: {endpoint.hostname} does not resolve.",
            "DNS resolution for this hostname is failing.",
            candidates, findings, actions,
        )

    addresses = (dns.data.get("addresses") if dns else []) or []
    known_ips = set(history.get("resolved_ips") or {})
    if addresses and known_ips and not known_ips & set(addresses):
        cand(
            "address_changed",
            "The target address changed",
            "DNS now points somewhere different from where checks used to succeed.",
        ).add(STRONG, f"was {', '.join(sorted(known_ips))}, now {', '.join(addresses)}")
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

    # ------------------------------------------------------------------
    # 3. TCP
    # ------------------------------------------------------------------
    if tcp and tcp.status == FAILED:
        attempts = tcp.data.get("attempts", [])
        refused = any("refused" in (a.get("error") or "").lower() for a in attempts)
        verdict = "connection_refused" if refused else "connection_timeout"

        if refused:
            cand(
                "service_not_listening",
                "Nothing is listening on the port",
                "The host answered the connection attempt with an active refusal, "
                "which means it is up but no process holds the port.",
            ).add(DIRECT, f"port {endpoint.port} actively refused the connection")
            findings.append(Finding(
                severity="high",
                title=f"Connection refused on port {endpoint.port}",
                detail=(
                    "The host is reachable and answered immediately - that is what "
                    "makes this a refusal rather than a timeout. Something is "
                    "routing packets to the host, but no process is bound to the port. "
                    "A crashed service, a container that exited, or a listener bound "
                    "to 127.0.0.1 instead of 0.0.0.0 all look exactly like this."
                ),
                action="Check whether the service is running and what it is bound to.",
            ))
        else:
            cand(
                "network_filtered",
                "A firewall or security group is dropping traffic",
                "Packets are being discarded silently rather than refused, which is "
                "what a filtering device does and what a stopped service does not.",
            ).add(DIRECT, f"port {endpoint.port} timed out on every address")
            findings.append(Finding(
                severity="high",
                title=f"Connection timed out on port {endpoint.port}",
                detail=(
                    "The connection was neither accepted nor refused - it was "
                    "silently dropped. A stopped service refuses immediately; a "
                    "firewall, security group or network ACL drops. This distinction "
                    "is the single most useful thing at this layer, because it "
                    "sends you to completely different places."
                ),
                action="Check security groups and firewall rules for this port and source.",
            ))
            cand(
                "service_not_listening",
                "Nothing is listening on the port",
                "Possible, but a stopped service normally refuses rather than times out.",
            ).add(CIRCUMSTANTIAL, "cannot be excluded without host access")

        if not history.get("ever_succeeded") and history.get("total_checks_recorded"):
            candidates[
                "service_not_listening" if refused else "network_filtered"
            ].add(SUPPORTING, "this endpoint has never passed a check")
            cand(
                "monitor_misconfigured",
                "The monitor is pointed at the wrong place",
                "An endpoint that has never once succeeded is more often a wrong "
                "host, port or scheme than a service that has been down since it "
                "was added.",
            ).add(STRONG, "no successful check has ever been recorded")

        actions.extend([
            Action(
                title="Confirm the port from another network",
                detail=(
                    "Rules out CertMonitor's own egress being the thing that "
                    "is blocked."
                ),
                risk=ActionRisk.SAFE.value,
                command=f"nc -vz -w 5 {endpoint.hostname} {endpoint.port}",
            ),
            Action(
                title=(
                    "Check the service is running and bound to a routable address"
                    if refused
                    else "Check the security group / firewall for this port"
                ),
                detail=(
                    "A listener on 127.0.0.1 refuses external connections while "
                    "looking perfectly healthy locally."
                    if refused
                    else "Silent drops are almost always a filtering rule, not the "
                    "application."
                ),
                risk=ActionRisk.SAFE.value,
                command="ss -ltnp | grep :%d" % endpoint.port if refused else None,
                command_note=(
                    "Run on the host that should be serving." if refused else None
                ),
            ),
            Action(
                title="Check the load balancer or ingress target",
                detail=(
                    "If traffic reaches an LB rather than the host directly, the "
                    "target group is where a healthy-looking service disappears."
                ),
                risk=ActionRisk.SAFE.value,
            ),
        ])
        return _finish_analysis(
            verdict,
            f"The host resolves but nothing is accepting connections on port {endpoint.port}.",
            (
                f"No process is listening on {endpoint.hostname}:{endpoint.port}."
                if refused
                else f"Traffic to {endpoint.hostname}:{endpoint.port} is being dropped, "
                "most likely by a firewall or security group."
            ),
            candidates, findings, actions,
        )

    if tcp and tcp.status == WARNING:
        cand(
            "partial_backend",
            "One backend address is unhealthy",
            "Some addresses accept connections and others do not, so requests "
            "succeed or fail depending on which one they land on.",
        ).add(DIRECT, tcp.detail)
        if history.get("state_transitions", 0) >= 4:
            candidates["partial_backend"].add(
                STRONG,
                f"{history['state_transitions']} up/down transitions in "
                f"{history['window_hours']}h match a partially-failing pool",
            )
        findings.append(Finding(
            severity="high",
            title="Some backend addresses are unreachable",
            detail=(
                f"{tcp.detail}. Requests balanced onto the failing address fail "
                "while others succeed - which is exactly what intermittent "
                "failure looks like from outside."
            ),
            action="Find and repair or remove the unhealthy pool member.",
        ))
        actions.append(Action(
            title="Check each address in the pool individually",
            detail=(
                "The healthy members mask the broken one in any aggregate view, "
                "which is why this only shows up as flapping."
            ),
            risk=ActionRisk.SAFE.value,
            command=f"dig +short {endpoint.hostname}",
            command_note=f"Then test each address: nc -vz -w 5 <address> {endpoint.port}",
        ))

    # ------------------------------------------------------------------
    # 4. TLS
    # ------------------------------------------------------------------
    if tls and tls.status == FAILED:
        d = tls.data
        days = d.get("days_remaining")

        if days is not None and days < 0:
            cand(
                "cert_expired",
                "The TLS certificate has expired",
                "Verifying clients refuse the connection outright; this is a hard "
                "failure independent of whether the application is healthy.",
            ).add(DIRECT, f"expired {abs(days)} day(s) ago")
            verdict = "cert_expired"
            findings.append(Finding(
                severity="high",
                title="The TLS certificate has expired",
                detail=(
                    f"It expired {abs(days)} day(s) ago ({d.get('expires_at')}). "
                    "Every client that verifies certificates - which is all of "
                    "them by default - will refuse to connect, whatever state the "
                    "application behind it is in."
                ),
                action="Renew and deploy the certificate, then re-run Diagnose.",
            ))
            actions.extend([
                Action(
                    title="Confirm which certificate is actually being served",
                    detail=(
                        "A renewed certificate that was never reloaded is the most "
                        "common version of this - the file on disk is new and the "
                        "process is still serving the old one from memory."
                    ),
                    risk=ActionRisk.SAFE.value,
                    command=(
                        f"openssl s_client -connect {endpoint.hostname}:{endpoint.port} "
                        f"-servername {endpoint.hostname} </dev/null 2>/dev/null "
                        f"| openssl x509 -noout -dates"
                    ),
                ),
                Action(
                    title="Renew and deploy the certificate",
                    detail="Then reload the terminator so it picks the new one up.",
                    risk=ActionRisk.DISRUPTIVE.value,
                    command_note=(
                        "A reload is usually graceful, but it does touch live "
                        "traffic - do it deliberately, not reflexively."
                    ),
                ),
            ])
        elif d.get("hostname_matches") is False:
            cand(
                "cert_hostname_mismatch",
                "The certificate does not cover this hostname",
                "The certificate is valid, but not for the name being requested.",
            ).add(DIRECT, f"issued for {d.get('common_name')}, requested as {endpoint.hostname}")
            verdict = "cert_hostname_mismatch"
            findings.append(Finding(
                severity="high",
                title="The certificate does not cover this hostname",
                detail=(
                    f"It is issued for {d.get('common_name')} but was requested as "
                    f"{endpoint.hostname}. Often this means the request is landing "
                    "on a default virtual host rather than the intended one."
                ),
                action=(
                    "Add the hostname to the certificate's SAN list, or point the "
                    "endpoint at the name the certificate covers."
                ),
            ))
            actions.append(Action(
                title="Check which names the served certificate covers",
                detail="SNI routing errors and missing SANs look identical from here.",
                risk=ActionRisk.SAFE.value,
                command=(
                    f"openssl s_client -connect {endpoint.hostname}:{endpoint.port} "
                    f"-servername {endpoint.hostname} </dev/null 2>/dev/null "
                    f"| openssl x509 -noout -text | grep -A1 'Subject Alternative Name'"
                ),
            ))
        elif d.get("self_signed"):
            cand(
                "cert_self_signed",
                "The certificate is self-signed",
                "No public CA vouches for it, so chain verification fails by design.",
            ).add(DIRECT, "the certificate is its own issuer")
            verdict = "cert_self_signed"
            findings.append(Finding(
                severity="medium",
                title="The certificate is self-signed",
                detail=(
                    "No public CA vouches for it. For an internal service this is "
                    "often intentional rather than broken."
                ),
                action=(
                    "If this is expected, turn off \"Verify the certificate chain\" "
                    "for this endpoint - expiry is still tracked."
                ),
            ))
            actions.append(Action(
                title="Decide whether this is intentional",
                detail=(
                    "If it is an internal CA, disable chain verification on this "
                    "endpoint. If it is not, the certificate needs replacing."
                ),
                risk=ActionRisk.SAFE.value,
            ))
        elif d.get("unverified_handshake"):
            cand(
                "cert_chain_incomplete",
                "The certificate chain is incomplete",
                "The handshake works but verification fails - classically a missing "
                "intermediate that browsers paper over and strict clients do not.",
            ).add(DIRECT, str(d.get("verification_error")))
            verdict = "cert_chain_incomplete"
            findings.append(Finding(
                severity="high",
                title="The certificate chain is incomplete or untrusted",
                detail=(
                    f"The handshake completes, so the service is up, but "
                    f"verification fails: {d.get('verification_error')}. A missing "
                    "intermediate is the usual cause. Browsers often fetch it "
                    "themselves and hide the problem; API clients do not."
                ),
                action="Serve the full chain: leaf plus intermediates.",
            ))
            actions.append(Action(
                title="Inspect the chain as served",
                detail="Count the certificates returned - a lone leaf is the tell.",
                risk=ActionRisk.SAFE.value,
                command=(
                    f"openssl s_client -connect {endpoint.hostname}:{endpoint.port} "
                    f"-servername {endpoint.hostname} -showcerts </dev/null"
                ),
            ))
        else:
            cand(
                "tls_handshake_failure",
                "The TLS handshake fails",
                "The connection is accepted but no TLS session can be established.",
            ).add(DIRECT, tls.detail)
            verdict = "tls_failure"
            findings.append(Finding(
                severity="high",
                title="TLS handshake failed",
                detail=tls.detail,
                action=(
                    "Check the protocol versions and ciphers the terminator offers - "
                    "a very old or misconfigured one can refuse a modern client."
                ),
            ))
            actions.append(Action(
                title="Check the offered protocols and ciphers",
                detail="A terminator restricted to TLS 1.0/1.1 fails modern clients.",
                risk=ActionRisk.SAFE.value,
                command=(
                    f"openssl s_client -connect {endpoint.hostname}:{endpoint.port} "
                    f"-servername {endpoint.hostname} -tls1_2 </dev/null"
                ),
            ))

        _score_change_correlation(candidates, closest_change, endpoint, tls_related=True)
        return _finish_analysis(
            verdict, f"TLS is the failing layer: {tls.detail}",
            candidates[max(candidates, key=lambda k: candidates[k].score)].explanation,
            candidates, findings, actions,
        )

    if tls and tls.status == OK:
        days = tls.data.get("days_remaining")
        if days is not None and days <= 30:
            severity = "high" if days <= 7 else "medium" if days <= 14 else "low"
            findings.append(Finding(
                severity=severity,
                title=f"The certificate expires in {days} day(s)",
                detail=(
                    f"Issued by {tls.data.get('issuer')}, expires "
                    f"{tls.data.get('expires_at')}. This is unrelated to any "
                    "current failure, but it will become one."
                ),
                action="Renew it now rather than at the deadline.",
            ))
            if days <= 14:
                actions.append(Action(
                    title=f"Renew the certificate - {days} day(s) left",
                    detail=(
                        "Independent of whatever else is wrong here. Renewal that "
                        "waits for the expiry date is renewal that happens during "
                        "an outage."
                    ),
                    risk=ActionRisk.SAFE.value,
                ))

    # ------------------------------------------------------------------
    # 5. HTTP
    # ------------------------------------------------------------------
    if http and http.status == FAILED:
        root = extras.get("root")
        insecure = extras.get("insecure")

        if insecure and insecure.get("status") == OK:
            cand(
                "cert_chain_incomplete",
                "The certificate is the problem, not the service",
                "The identical request succeeds with verification disabled.",
            ).add(DIRECT, f"succeeds unverified with HTTP {insecure.get('http_status')}")
            findings.append(Finding(
                severity="high",
                title="The request only succeeds with verification disabled",
                detail=(
                    f"With chain verification off the endpoint answers HTTP "
                    f"{insecure.get('http_status')}. That isolates it cleanly: the "
                    "service is up and the certificate is what is broken."
                ),
                action="Fix the certificate chain, or disable verification here.",
            ))

        if code is None:
            cand(
                "application_not_responding",
                "The application accepted the connection but never replied",
                "TCP and TLS completed, so the listener is alive - the process "
                "behind it is stuck rather than absent.",
            ).add(DIRECT, http.detail)
            cand(
                "resource_saturation",
                "Resource saturation or thread/connection pool exhaustion",
                "A process that accepts connections and then never answers is "
                "usually out of workers, connections or memory. CertMonitor "
                "cannot measure this - it is inference from the shape of the failure.",
            ).add(SUPPORTING, "accepting but not answering is the classic signature")
            _score_change_correlation(candidates, closest_change, endpoint)
            findings.append(Finding(
                severity="high",
                title="The HTTP request did not complete",
                detail=(
                    "TCP and TLS succeeded, so something is listening and "
                    "terminating TLS, but no HTTP response came back. The process "
                    "is stuck, not gone."
                ),
                action=(
                    "Check application logs and thread/connection pool metrics "
                    "rather than whether the service is running."
                ),
            ))
            actions.extend(_application_actions(endpoint, closest_change))
            return _finish_analysis(
                "http_no_response",
                "The transport is healthy but the application never answered.",
                "The application is accepting connections but not responding to them.",
                candidates, findings, actions,
            )

        # ---- 502 / 503 / 504: the edge is fine, its upstream is not
        if code in (502, 503, 504):
            cand(
                "upstream_unavailable",
                "The reverse proxy cannot reach its upstream",
                f"HTTP {code} is generated by the proxy or load balancer itself, "
                "not by the application - the edge is healthy and what sits behind "
                "it is not.",
            ).add(DIRECT, f"HTTP {code} returned by the edge")
            if tls and tls.status == OK:
                candidates["upstream_unavailable"].add(
                    SUPPORTING, "TLS terminated cleanly, so the edge is serving normally"
                )
            if code == 504:
                cand(
                    "upstream_slow",
                    "The upstream is responding too slowly",
                    "504 specifically means the proxy gave up waiting, which points "
                    "at slowness rather than absence.",
                ).add(STRONG, "504 is a gateway timeout, not a refusal")
            _score_change_correlation(candidates, closest_change, endpoint)
            _score_recurrence(candidates, incidents, "upstream_unavailable")

            findings.append(Finding(
                severity="high",
                title=f"The proxy returned HTTP {code}",
                detail=(
                    f"{code} comes from the reverse proxy or load balancer, not the "
                    "application. It means the upstream is unreachable, overloaded "
                    "or timing out. Everything in front is working, which is why "
                    "DNS, TCP and TLS all passed."
                ),
                action="Check the application instances and the proxy's upstream health.",
            ))
            actions.extend(_application_actions(endpoint, closest_change))
            return _finish_analysis(
                "upstream_unavailable",
                f"The edge is serving but its upstream is failing (HTTP {code}).",
                (
                    "The reverse proxy or load balancer is healthy but cannot get a "
                    "response from the application behind it."
                ),
                candidates, findings, actions,
            )

        # ---- 404
        if code == 404:
            root_ok = root and root.get("status") == OK and root.get("http_status") != 404
            if root_ok:
                cand(
                    "wrong_path",
                    "The monitored path does not exist",
                    "The host serves normally at / but not at the path being checked.",
                ).add(DIRECT, f"/ returns {root.get('http_status')}, {endpoint.path} returns 404")
                findings.append(Finding(
                    severity="high",
                    title="The path is wrong, not the service",
                    detail=(
                        f"{endpoint.path} returns 404 while / returns "
                        f"{root.get('http_status')}. The host is serving; the "
                        "monitored path is not routed."
                    ),
                    action="Point the endpoint at a real health path.",
                ))
                actions.append(Action(
                    title="Point the monitor at a path that exists",
                    detail=(
                        "CertMonitor can find it for you: turn on health-path "
                        "discovery under Settings -> Monitoring and it will try "
                        "/health, /healthz, /ready, /actuator/health and adopt "
                        "whichever answers."
                    ),
                    risk=ActionRisk.SAFE.value,
                    command=f"curl -sSI --max-time 10 {endpoint.url}",
                ))
                return _finish_analysis(
                    "wrong_path",
                    "The service is up - the monitored path returns 404.",
                    "The endpoint is configured with a path the service does not serve.",
                    candidates, findings, actions,
                )

            cand(
                "wrong_path",
                "The monitored path does not exist",
                "The server answers, so it is up, but nothing is routed here.",
            ).add(STRONG, "HTTP 404 at the monitored path")
            cand(
                "application_error",
                "The application is not routing correctly",
                "A 404 across the whole host can also mean the application failed "
                "to start its routes.",
            ).add(CIRCUMSTANTIAL, "the site root does not answer normally either")
            _score_change_correlation(candidates, closest_change, endpoint)
            findings.append(Finding(
                severity="medium",
                title="HTTP 404 at the monitored URL",
                detail=(
                    "The server is answering, so it is up, but nothing is routed at "
                    "this path. Many APIs legitimately 404 at their root."
                ),
                action=(
                    "Monitor a real health path, or add 404 to the expected status "
                    "codes if 404 is correct here."
                ),
            ))
            actions.append(Action(
                title="Enable automatic health-path discovery",
                detail=(
                    "Settings -> Monitoring. CertMonitor will try the common health "
                    "paths and adopt the first that answers, leaving your configured "
                    "URL unchanged."
                ),
                risk=ActionRisk.SAFE.value,
            ))
            return _finish_analysis(
                "likely_wrong_path_or_expectation",
                "The host answers but returns 404 at the monitored path.",
                "The monitored path is probably wrong.",
                candidates, findings, actions,
            )

        # ---- 401 / 403
        if code in (401, 403):
            cand(
                "auth_required",
                "The endpoint requires authentication",
                "The service is up and correctly rejecting an unauthenticated "
                "request. That is a monitor configuration mismatch, not an outage.",
            ).add(DIRECT, f"HTTP {code} with no credentials configured"
                  if endpoint.auth_type == "none" else f"HTTP {code} with credentials configured")
            if endpoint.auth_type != "none":
                cand(
                    "credentials_rejected",
                    "The configured credentials are being rejected",
                    "Credentials are set on this endpoint and are still not "
                    "getting through - expired, rotated, or the wrong scheme.",
                ).add(STRONG, f"auth is configured as '{endpoint.auth_type}' and still got {code}")
            findings.append(Finding(
                severity="medium",
                title=f"The endpoint requires authentication (HTTP {code})",
                detail=(
                    "The service is up and rejecting an unauthenticated request. "
                    + (
                        "No credentials are configured for this endpoint."
                        if endpoint.auth_type == "none"
                        else f"Credentials are configured ({endpoint.auth_type}) but "
                        "are being rejected - check whether they have been rotated."
                    )
                ),
                action=(
                    "Add or update the credentials under the endpoint's "
                    f"authentication settings, or add {code} to the expected status "
                    "codes if an auth challenge is the correct healthy response."
                ),
            ))
            actions.append(Action(
                title=(
                    "Add credentials to the endpoint"
                    if endpoint.auth_type == "none"
                    else "Re-enter the endpoint's credentials"
                ),
                detail=(
                    "They are stored encrypted and never returned by the API or "
                    "included in exports."
                ),
                risk=ActionRisk.SAFE.value,
            ))
            return _finish_analysis(
                "auth_required",
                f"The service is up but requires authentication (HTTP {code}).",
                "The monitor is not authenticating successfully against a healthy service.",
                candidates, findings, actions,
            )

        # ---- 429
        if code == 429:
            cand(
                "rate_limited",
                "The monitor is being rate limited",
                "The service is healthy and is deliberately throttling this client.",
            ).add(DIRECT, "HTTP 429 Too Many Requests")
            findings.append(Finding(
                severity="medium",
                title="The endpoint is rate limiting the monitor",
                detail=(
                    "HTTP 429 means the service is up and working - it is refusing "
                    f"this client specifically. A {endpoint.interval_seconds}s check "
                    "interval may be tripping a limit, especially with several "
                    "endpoints on the same host."
                ),
                action="Lengthen the check interval, or exempt the monitor's source address.",
            ))
            actions.append(Action(
                title="Lengthen the check interval for this endpoint",
                detail=(
                    f"Currently every {endpoint.interval_seconds}s. Monitoring that "
                    "trips a rate limit is monitoring that generates its own alerts."
                ),
                risk=ActionRisk.SAFE.value,
            ))
            return _finish_analysis(
                "rate_limited",
                "The service is healthy but is rate limiting the monitor (HTTP 429).",
                "CertMonitor is being throttled by the service it is checking.",
                candidates, findings, actions,
            )

        # ---- other 5xx
        if 500 <= code < 600:
            cand(
                "application_error",
                "The application is failing internally",
                "The request reached the application and it failed while handling "
                "it - infrastructure delivered it correctly.",
            ).add(DIRECT, f"HTTP {code} from the application")
            _score_change_correlation(candidates, closest_change, endpoint)
            _score_recurrence(candidates, incidents, "application_error")
            if app_summary and app_summary["total"] > 1 and app_summary["down"] >= app_summary["total"]:
                candidates["application_error"].add(
                    STRONG,
                    f"all {app_summary['total']} endpoints of "
                    f"{app_summary['application']} are down together",
                )
            findings.append(Finding(
                severity="high",
                title=f"The application returned HTTP {code}",
                detail=(
                    "DNS, TCP and TLS all passed and the request was delivered - "
                    "the application received it and failed internally. This is an "
                    "application fault, not infrastructure."
                ),
                action="Read the application log for this timestamp.",
            ))
            actions.extend(_application_actions(endpoint, closest_change))
            return _finish_analysis(
                "application_error",
                f"The application is failing with HTTP {code}.",
                "The application received the request and errored while handling it.",
                candidates, findings, actions,
            )

        # ---- 3xx
        if 300 <= code < 400:
            cand(
                "unexpected_redirect",
                "The endpoint redirects",
                "Not a failure of the service - a mismatch between what it does "
                "and what the monitor expects.",
            ).add(DIRECT, f"HTTP {code} to {http.data.get('location') or 'an unstated location'}")
            findings.append(Finding(
                severity="low",
                title=f"The endpoint redirects (HTTP {code})",
                detail=f"Location: {http.data.get('location') or 'not sent'}.",
                action=(
                    "Enable \"Follow redirects\", monitor the final URL directly, or "
                    f"add {code} to the expected status codes."
                ),
            ))
            actions.append(Action(
                title="Follow the redirect or monitor its destination",
                detail=(
                    "Monitoring the redirect rather than the destination means an "
                    "outage behind it would go unnoticed."
                ),
                risk=ActionRisk.SAFE.value,
                command=f"curl -sSIL --max-time 15 {endpoint.url}",
                command_note="-L follows the chain and shows where it lands.",
            ))
            return _finish_analysis(
                "unexpected_redirect", f"The endpoint redirects with HTTP {code}.",
                "The monitored URL redirects and the monitor is not following it.",
                candidates, findings, actions,
            )

        cand(
            "status_expectation_mismatch",
            "The status is not what the monitor expects",
            "The service answers consistently; the expectation configured here "
            "does not match it.",
        ).add(DIRECT, f"HTTP {code}, expected {endpoint.expected_status_codes}")
        findings.append(Finding(
            severity="medium",
            title=f"Unexpected HTTP {code}",
            detail=f"Expected one of {endpoint.expected_status_codes}.",
            action="Either fix the service or update the expected status codes.",
        ))
        return _finish_analysis(
            "http_status_mismatch",
            f"The service answers with HTTP {code}, which is not expected.",
            "The response is stable but does not match the configured expectation.",
            candidates, findings, actions,
        )

    # ------------------------------------------------------------------
    # 6. Everything probed clean. That is not the end of the analysis.
    # ------------------------------------------------------------------
    if not history.get("ever_succeeded") and history.get("total_checks_recorded"):
        cand(
            "monitor_misconfigured",
            "The monitor has never worked",
            "Every recorded check has failed, which is far more often a "
            "configuration error than a service down since the day it was added.",
        ).add(STRONG, "no successful check has ever been recorded")
        findings.append(Finding(
            severity="high",
            title="This endpoint has never passed a check",
            detail=(
                "Every recorded check has failed. A monitor that was wrong from "
                "the start is far more often a configuration error than a service "
                "that has been down the whole time."
            ),
            action="Re-read the URL, port, expected status and authentication settings.",
        ))

    availability = history.get("recent_availability_pct")
    transitions = history.get("state_transitions", 0)
    intermittent = (availability is not None and availability < 95) or transitions >= 6

    if intermittent:
        cand(
            "intermittent_backend",
            "Intermittent failure",
            "The endpoint answers now, but not reliably. A single passing probe "
            "says nothing about a service that fails one request in four.",
        ).add(DIRECT, f"{availability}% of the last {len(history.get('recent_checks') or [])} checks passed"
              if availability is not None else f"{transitions} state changes in {history['window_hours']}h")
        if tcp and tcp.status == WARNING:
            candidates["intermittent_backend"].add(
                STRONG, "one backend address is unreachable, which explains the pattern"
            )
        else:
            cand(
                "partial_backend",
                "One unhealthy member behind a load balancer",
                "The usual cause of alternating success and failure: requests land "
                "on different backends and only some of them work.",
            ).add(SUPPORTING, "the pattern matches a partially-failing pool")
            cand(
                "timeout_too_tight",
                "The timeout is close to the real response time",
                f"A {endpoint.timeout_seconds}s timeout against a service that "
                "normally takes near that long fails whenever it is slightly slower.",
            ).add(CIRCUMSTANTIAL, "worth excluding before chasing the backend")
        _score_recurrence(candidates, incidents, "intermittent_backend")
        findings.append(Finding(
            severity="high",
            title=(
                f"Intermittent failure - {availability}% availability over the last "
                f"{len(history.get('recent_checks') or [])} checks"
                if availability is not None
                else f"The endpoint is flapping ({transitions} state changes in "
                f"{history['window_hours']}h)"
            ),
            detail=(
                "It responds correctly right now, which is exactly why this is "
                "worth surfacing - a single passing probe would have called it "
                "healthy. Alternating success and failure usually means one "
                "unhealthy member behind a load balancer, or a timeout set close "
                "to the real response time."
            ),
            action=(
                "Test each backend address individually, and compare the timeout "
                "against the observed response time."
            ),
        ))
        actions.extend([
            Action(
                title="Test each backend address separately",
                detail="The healthy members hide the broken one in any aggregate view.",
                risk=ActionRisk.SAFE.value,
                command=f"dig +short {endpoint.hostname}",
                command_note=(
                    f"Then, for each address: curl -sS --resolve "
                    f"{endpoint.hostname}:{endpoint.port}:<address> -o /dev/null "
                    f"-w '%{{http_code}}\\n' {endpoint.url}"
                ),
            ),
            Action(
                title="Compare the timeout with the real response time",
                detail=(
                    f"Timeout is {endpoint.timeout_seconds}s; the recent baseline is "
                    f"{history.get('baseline_response_time_ms') or 'unknown'} ms. "
                    "If those are close, the monitor is manufacturing failures."
                ),
                risk=ActionRisk.SAFE.value,
            ),
        ])

    ratio = history.get("latency_ratio")
    if ratio and ratio >= LATENCY_ANOMALY_RATIO:
        baseline = history.get("baseline_response_time_ms")
        current = history.get("current_response_time_ms")
        cand(
            "performance_degradation",
            "Performance degradation",
            "Still returning a correct response, but much slower than it normally "
            "does. Degradation reliably precedes failure.",
        ).add(DIRECT, f"{current:.0f} ms against a {baseline:.0f} ms baseline ({ratio:.1f}x)")
        cand(
            "resource_saturation",
            "Resource or dependency saturation",
            "The usual causes of a uniform slowdown - CPU, memory, database "
            "latency or a slow dependency. CertMonitor cannot measure any of "
            "them; this is inference from the timing alone.",
        ).add(SUPPORTING, "a uniform slowdown with correct responses fits saturation")
        _score_change_correlation(candidates, closest_change, endpoint)
        findings.append(Finding(
            severity="medium",
            title=(
                f"Response time is {ratio:.1f}x baseline "
                f"({current:.0f} ms vs {baseline:.0f} ms)"
            ),
            detail=(
                "The endpoint still returns a correct response, so it is not down "
                "and would not alert on status alone. A sustained slowdown of this "
                "size is usually the first visible symptom of saturation."
            ),
            action=(
                "Check resource usage and dependency latency on the host before "
                "this becomes an outage."
            ),
        ))
        actions.append(Action(
            title="Look at the timing breakdown to localise the slowness",
            detail=(
                "The history tab separates DNS, connect, TLS and TTFB. Slow TTFB "
                "with fast connect is the application or its dependencies; slow "
                "connect is the network."
            ),
            risk=ActionRisk.SAFE.value,
            command=(
                f"curl -sS -o /dev/null -w 'dns %{{time_namelookup}} connect "
                f"%{{time_connect}} tls %{{time_appconnect}} ttfb "
                f"%{{time_starttransfer}} total %{{time_total}}\\n' {endpoint.url}"
            ),
        ))

    # Only a candidate that describes an ongoing problem changes the verdict.
    # Incidental observations - a changed address, a certificate with weeks
    # left - are worth reporting as findings but must not turn a working
    # endpoint into a suspect one.
    substantive = {
        "intermittent_backend",
        "partial_backend",
        "performance_degradation",
        "resource_saturation",
        "monitor_misconfigured",
        "timeout_too_tight",
    }
    ongoing = [
        c for c in candidates.values() if c.cause in substantive and c.score > 0
    ]
    if ongoing:
        leader = max(ongoing, key=lambda c: c.score)
        verdict = (
            "intermittent_failure"
            if leader.cause in ("intermittent_backend", "partial_backend", "timeout_too_tight")
            else "performance_degradation"
            if leader.cause in ("performance_degradation", "resource_saturation")
            else "configuration_suspect"
        )
        return _finish_analysis(
            verdict,
            (
                "Every layer responds correctly right now, but the recent record "
                "does not - a single passing probe would have called this healthy."
            ),
            leader.explanation,
            candidates, findings, actions,
        )

    if endpoint.current_status == EndpointStatus.DOWN.value:
        actions.append(Action(
            title="Confirm the recovery holds",
            detail=(
                "One passing probe is not a recovery. Watch the next few scheduled "
                "checks before closing anything."
            ),
            risk=ActionRisk.SAFE.value,
        ))
        return _finish_analysis(
            "recovered_since_last_check",
            "Every layer responds correctly now. The endpoint appears to have "
            "recovered since its last scheduled check.",
            "The failure is no longer reproducible.",
            candidates, findings, actions,
        )

    return _finish_analysis(
        "healthy", "All layers responded correctly.", None,
        candidates, findings, actions,
    )


# ------------------------------------------------------ scoring helpers
def _score_change_correlation(
    candidates: dict[str, Candidate],
    closest: dict[str, Any] | None,
    endpoint: Endpoint,
    *,
    tls_related: bool = False,
) -> None:
    """Weigh a nearby deployment into the candidate set.

    Deliberately a *separate candidate* rather than a boost to the others: a
    deployment is a suspect in its own right, and merging it into "application
    error" would hide the single most actionable fact - that there is
    something specific to roll back.

    The weight decays with distance in time, because a release that finished
    two minutes before the failure is a very different signal from one that
    finished eighty minutes before.
    """
    if not closest:
        return

    gap = closest.get("minutes_before_failure")
    if gap is None:
        return

    if gap <= 10:
        weight, wording = DIRECT, "immediately before"
    elif gap <= 30:
        weight, wording = STRONG, "shortly before"
    else:
        weight, wording = CIRCUMSTANTIAL, "not long before"

    candidate = candidates.setdefault(
        "deployment_related",
        Candidate(
            cause="deployment_related",
            label=f"The {closest['reference']} deployment",
            explanation=(
                "The failure began shortly after a deployment completed. That is "
                "a correlation in time, not proof of cause - but it is the "
                "strongest available lead and the easiest to test, because it can "
                "be rolled back."
            ),
        ),
    )
    candidate.add(
        weight,
        f"{closest['reference']} completed {gap:.0f} minutes {wording} the failure "
        f"started ({closest['application']}"
        + (f" / {closest['environment']}" if closest.get("environment") else "")
        + ")",
    )
    if closest.get("risk") == "high":
        candidate.add(SUPPORTING, "the change was recorded as high risk")
    if tls_related:
        candidate.add(
            CIRCUMSTANTIAL,
            "a deployment that replaces a certificate can break the chain",
        )


def _score_recurrence(
    candidates: dict[str, Candidate],
    incidents: dict[str, Any],
    cause: str,
) -> None:
    """A repeat offender is evidence about the cause, not just a statistic."""
    count = incidents.get("incident_count", 0)
    if count < 3 or cause not in candidates:
        return
    candidates[cause].add(
        SUPPORTING,
        f"{count} incidents on this endpoint in the last "
        f"{incidents['window_days']} days - a recurring fault, not a one-off",
    )


def _application_actions(
    endpoint: Endpoint, closest: dict[str, Any] | None
) -> list[Action]:
    """The standard application-layer investigation, ordered by risk.

    Safest first, then most likely, then most impactful - so an engineer who
    stops after step two has still done the sensible thing. Nothing here is
    executed by CertMonitor; the container commands are explicitly conditional
    because CertMonitor cannot see whether a container is involved at all.
    """
    steps = [
        Action(
            title="Read the application log around the failure",
            detail=(
                "CertMonitor never stores response bodies, so the application's "
                "own log is the only place the reason exists."
            ),
            risk=ActionRisk.SAFE.value,
            command="kubectl logs <pod> -n <namespace> --tail=200",
            command_note=(
                "IF this runs on Kubernetes. Add --previous to read the container "
                "that died, which is where a crash loop explains itself."
            ),
        ),
        Action(
            title="Check whether the process is actually up",
            detail=(
                "Distinguish 'crashed' from 'running but failing' before doing "
                "anything else - they lead in opposite directions."
            ),
            risk=ActionRisk.SAFE.value,
            command="docker ps -a --filter name=<container>",
            command_note="IF this runs in Docker on a host you control.",
        ),
        Action(
            title="Check the proxy's upstream health",
            detail=(
                "The load balancer or ingress knows which backends it considers "
                "healthy, and its own error log usually names the failing address."
            ),
            risk=ActionRisk.SAFE.value,
        ),
    ]

    if closest:
        steps.append(Action(
            title=f"Compare against what {closest['reference']} changed",
            detail=(
                f"It completed {closest['minutes_before_failure']:.0f} minutes "
                "before the failure started. Its notes and rollback plan are on "
                "the change record. Correlation, not proof - but the cheapest "
                "hypothesis to test."
            ),
            risk=ActionRisk.SAFE.value,
        ))
        steps.append(Action(
            title=f"Roll back {closest['reference']} if the timing holds up",
            detail=(
                "Only once the log has been read. Rolling back on timing alone "
                "discards the evidence of what actually broke, and if the "
                "deployment was not the cause you have taken a second outage for "
                "nothing."
            ),
            risk=ActionRisk.HIGH_RISK.value,
            command_note=(
                "CertMonitor will not do this for you. Raise it as a change so "
                "the rollback is recorded and monitoring is paused for it."
            ),
        ))
    else:
        steps.append(Action(
            title="Restart the application only after reading the logs",
            detail=(
                "A restart usually clears the symptom and always destroys the "
                "evidence. Capture the logs first or the next occurrence starts "
                "from nothing."
            ),
            risk=ActionRisk.DISRUPTIVE.value,
        ))
    return steps


def _finish_analysis(
    verdict: str,
    summary: str,
    root_cause: str | None,
    candidates: dict[str, Candidate],
    findings: list[Finding],
    actions: list[Action],
) -> dict[str, Any]:
    ranked, confidence = rank(list(candidates.values()))
    return {
        "verdict": verdict,
        "summary": summary,
        "root_cause": (ranked[0]["explanation"] if ranked else root_cause),
        "candidates": ranked,
        "confidence": confidence,
        "findings": findings,
        "actions": actions,
    }


# ------------------------------------------------------------- entrypoint
async def diagnose(
    session: AsyncSession,
    endpoint: Endpoint,
    *,
    focus: str = DiagnosisFocus.AUTO.value,
    user: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run a full triage of one endpoint.

    Writes nothing to the monitoring history - diagnosing an endpoint must
    never distort its uptime figures. The conclusion *is* stored, in its own
    table, so that "has this happened before?" can be answered next time.
    """
    started = perf_counter()

    secret = (
        decrypt_secret(endpoint.auth_secret_encrypted)
        if endpoint.auth_secret_encrypted
        else None
    )

    # ---------------------------------------------------- live probing
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

    # ------------------------------------------------ stored evidence
    history = await _history(session, endpoint)
    correlation = await _correlation(session, endpoint)
    onset = await _failure_onset(session, endpoint)
    changes = await _change_correlation(session, endpoint, onset)
    incidents = await _incident_correlation(session, endpoint)
    recurrence = await _recurrence(session, endpoint)
    app_summary = await _application_summary(session, endpoint)

    # ------------------------------------------------------ reasoning
    result = _analyse(
        endpoint, layers, extras, history, correlation,
        changes, incidents, app_summary,
    )
    verdict = result["verdict"]
    findings: list[Finding] = result["findings"]
    actions: list[Action] = result["actions"]

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

    # ---- recurring problems are worth saying out loud
    if recurrence["most_common_verdict_count"] >= 3:
        findings.append(Finding(
            severity="medium",
            title=(
                f"Recurring problem: diagnosed as "
                f"'{recurrence['most_common_verdict']}' "
                f"{recurrence['most_common_verdict_count']} times in the last "
                f"{recurrence['window_days']} days"
            ),
            detail=(
                "The same conclusion keeps coming back, which means whatever was "
                "done last time treated the symptom rather than the cause."
                + (
                    " Previously resolved by: "
                    + "; ".join(
                        r["resolution"] for r in recurrence["past_resolutions"][:2]
                    )
                    if recurrence["past_resolutions"]
                    else " No resolution was ever recorded, so there is nothing to "
                    "learn from the previous occurrences - record one this time."
                )
            ),
            action=(
                "Treat this as a standing problem rather than an incident, and "
                "record what actually fixed it."
            ),
        ))

    if incidents["incident_count"] >= 3:
        findings.append(Finding(
            severity="medium",
            title=(
                f"{incidents['incident_count']} incidents in the last "
                f"{incidents['window_days']} days"
            ),
            detail=(
                f"Most common cause: {incidents['most_common_reason']} "
                f"({incidents['most_common_reason_count']} of them)."
                + (
                    f" Median outage {incidents['median_duration_seconds'] // 60} "
                    "minutes."
                    if incidents.get("median_duration_seconds")
                    else ""
                )
            ),
            action="Look for the pattern rather than fixing each occurrence.",
        ))

    # ------------------------------------------------------- severity
    environment_name = endpoint.environment.name if endpoint.environment else None
    is_production = (environment_name or "").lower() in ("production", "prod")
    severity = severity_for(
        verdict=verdict,
        endpoint=endpoint,
        is_production=is_production,
        availability_pct=history.get("recent_availability_pct"),
        days_to_expiry=(tls.data.get("days_remaining") if tls else None),
        latency_ratio=history.get("latency_ratio"),
        application_down=is_total_application_outage(app_summary),
    )

    # --------------------------------------------- deepest layer that worked
    deepest = None
    for name in ("dns", "tcp", "tls", "http"):
        if layers[name].status in (OK, WARNING):
            deepest = name
        elif layers[name].status == FAILED:
            break

    evidence = _build_evidence(
        endpoint, layers, history, changes, incidents, app_summary
    )
    unknowns = blind_spots(has_change_data=bool(changes.get("recent")))


    elapsed = round((perf_counter() - started) * 1000, 1)

    payload = {
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "url": endpoint.url,
        "application": endpoint.application,
        "environment": environment_name,
        "current_status": endpoint.current_status,
        "generated_at": datetime.now(timezone.utc),
        "elapsed_ms": elapsed,
        "focus": focus,

        "verdict": verdict,
        "summary": result["summary"],
        "root_cause": result["root_cause"],
        "confidence": result["confidence"],
        "severity": severity,
        "deepest_layer_ok": deepest,
        "failure_started_at": onset,

        "candidates": result["candidates"],
        "evidence": [e.as_dict() for e in evidence],
        "not_observable": [e.as_dict() for e in unknowns],
        "actions": [a.as_dict(index + 1) for index, a in enumerate(actions)],
        "commands": diagnostic_commands(endpoint, verdict),
        "verification": verification_plan(
            endpoint, verdict,
            baseline_ms=history.get("baseline_response_time_ms"),
        ),

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
        "changes": changes,
        "incidents": incidents,
        "recurrence": recurrence,
        "application_summary": app_summary,
    }

    if persist:
        payload["diagnosis_id"] = await _store(session, endpoint, payload, user=user)

    logger.info(
        "endpoint_diagnosed",
        endpoint=endpoint.name,
        verdict=verdict,
        severity=severity,
        confidence=result["confidence"],
        deepest_layer_ok=deepest,
        candidates=len(result["candidates"]),
        elapsed_ms=elapsed,
    )
    return payload


async def _store(
    session: AsyncSession,
    endpoint: Endpoint,
    payload: dict[str, Any],
    *,
    user: Any | None,
) -> int:
    """Persist the conclusion so recurrence can be detected next time.

    Only the conclusion: the layer payloads are large, stale within minutes,
    and would grow this table without bound for no diagnostic benefit.
    """
    from app.models.diagnosis import Diagnosis

    changes = payload.get("changes") or {}
    closest = changes.get("closest") or changes.get("active_deployment")
    open_incident = (payload.get("incidents") or {}).get("open_incident")
    http_layer = next(
        (layer for layer in payload["layers"] if layer["layer"] == "http"), None
    )

    record = Diagnosis(
        endpoint_id=endpoint.id,
        endpoint_name=endpoint.name,
        application=endpoint.application,
        requested_by_id=getattr(user, "id", None),
        requested_by=getattr(user, "username", None),
        focus=payload.get("focus") or DiagnosisFocus.AUTO.value,
        verdict=payload["verdict"],
        severity=payload["severity"],
        confidence=payload["confidence"],
        headline=payload["summary"],
        root_cause=payload.get("root_cause"),
        endpoint_status=endpoint.current_status,
        deepest_layer_ok=payload.get("deepest_layer_ok"),
        http_status_code=(http_layer or {}).get("data", {}).get("http_status"),
        response_time_ms=payload.get("history", {}).get("current_response_time_ms"),
        candidates=payload.get("candidates"),
        actions=payload.get("actions"),
        incident_id=(open_incident or {}).get("id"),
        change_id=(closest or {}).get("id"),
    )
    session.add(record)
    await session.flush()
    return record.id


async def record_resolution(
    session: AsyncSession,
    diagnosis: Any,
    *,
    resolution: str,
    user: Any | None = None,
) -> Any:
    """Note what actually fixed it.

    This is the field that turns a pile of diagnoses into something the next
    engineer can use. The engine surfaces it verbatim the next time the same
    verdict comes back on this endpoint.
    """
    diagnosis.resolution = resolution.strip()
    diagnosis.resolved_at = datetime.now(timezone.utc)
    diagnosis.resolved_by = getattr(user, "username", None)
    await session.flush()
    return diagnosis
