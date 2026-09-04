"""The reasoning layer of Diagnose.

:mod:`app.services.diagnostics_service` gathers evidence - live layer probes,
monitoring history, incidents, deployments, sibling endpoints. This module
turns that evidence into the thing an engineer actually needs at 2am: a ranked
set of probable causes, the reasoning behind each, what to do about it, and how
to tell whether the fix worked.

Three rules govern everything here.

**Rank causes, do not pick one.** A 502 four minutes after a deployment has an
obvious leading explanation and two plausible others. Presenting only the
leader hides the fact that it might be wrong. Every candidate carries the
evidence that scored it, so a disagreeing engineer can see exactly which
signal to challenge.

**Confidence comes from the evidence, not from tone.** The band is computed
from how much independent evidence supports the leader and how far ahead of
the runner-up it is. Two signals pointing the same way is Medium however
confidently the sentence reads.

**Never invent infrastructure.** InfraSight observes an endpoint from the
outside. It has no view of pods, containers, CPU or databases, and says so
explicitly rather than producing a plausible guess. Every statement is tagged
``observed``, ``inferred`` or ``unknown`` - see :class:`Evidence`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import (
    ActionRisk,
    Confidence,
    DiagnosisSeverity,
    EndpointStatus,
    EvidenceKind,
)

OK = "ok"
FAILED = "failed"
WARNING = "warning"
SKIPPED = "skipped"


# --------------------------------------------------------------- structures
@dataclass
class Evidence:
    """One fact, and where it came from.

    ``kind`` is the honesty mechanism. ``observed`` was measured or read from
    the database; ``inferred`` is a conclusion drawn from observations;
    ``unknown`` names something that matters but is outside what InfraSight
    can see, so the operator knows to look themselves rather than assuming it
    was checked.
    """

    label: str
    value: str
    kind: str = EvidenceKind.OBSERVED.value
    status: str | None = None       # ok | warning | failed | None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Candidate:
    """A possible root cause and the evidence weight behind it."""

    cause: str
    label: str
    explanation: str
    score: int = 0
    why: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> "Candidate":
        self.score += points
        self.why.append(reason)
        return self


@dataclass
class Action:
    """Something to do about it, with its blast radius stated."""

    title: str
    detail: str
    risk: str = ActionRisk.SAFE.value
    command: str | None = None
    command_note: str | None = None

    def as_dict(self, step: int) -> dict[str, Any]:
        return {
            "step": step,
            "title": self.title,
            "detail": self.detail,
            "risk": self.risk,
            "command": self.command,
            "command_note": self.command_note,
        }


# Scoring weights. Kept in one place so the model can be read at a glance
# rather than reverse-engineered from the branches below.
#
# DIRECT is a probe that observed the failure itself and admits few other
# readings - a refused TCP connection, an expired certificate. SUPPORTING is
# real but weaker evidence: a matching pattern in history, a sibling endpoint
# failing the same way. CIRCUMSTANTIAL is suggestive only - a deployment that
# finished nearby in time proves nothing on its own.
DIRECT = 50
STRONG = 30
SUPPORTING = 15
CIRCUMSTANTIAL = 8


def _confidence(ranked: list[Candidate]) -> str:
    """Confidence in the leading candidate.

    Two things have to hold for High: the leader must rest on more than one
    piece of evidence, and it must be clearly ahead. A single observation that
    happens to have no competition is Medium at best - one probe can be
    misleading, and saying High on the strength of it is exactly the
    overconfidence that sends someone down the wrong path.
    """
    if not ranked:
        return Confidence.LOW.value

    leader = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0
    margin = leader.score - runner_up
    signals = len(leader.why)

    if leader.score >= DIRECT and signals >= 2 and margin >= STRONG:
        return Confidence.HIGH.value
    if leader.score >= DIRECT and signals >= 3:
        return Confidence.HIGH.value
    if leader.score >= STRONG and margin > 0:
        return Confidence.MEDIUM.value
    if leader.score >= STRONG:
        return Confidence.MEDIUM.value
    return Confidence.LOW.value


def _band(index: int, share: float) -> str:
    """Words, not false precision, for how a candidate ranks."""
    if index == 0:
        return "most_likely"
    if share >= 0.15:
        return "possible"
    return "less_likely"


def rank(candidates: list[Candidate]) -> tuple[list[dict[str, Any]], str]:
    """Order candidates and express each as a share of total evidence weight.

    The share is exactly that - a proportion of accumulated weight - and is
    labelled as such in the UI. It is not a probability, and pretending
    otherwise would put a decimal point on a handful of heuristics.
    """
    live = sorted(
        (c for c in candidates if c.score > 0), key=lambda c: c.score, reverse=True
    )
    total = sum(c.score for c in live) or 1

    ranked = [
        {
            "cause": c.cause,
            "label": c.label,
            "explanation": c.explanation,
            "score": c.score,
            "share": round(c.score / total, 3),
            "band": _band(index, c.score / total),
            "why": c.why,
        }
        for index, c in enumerate(live)
    ]
    return ranked, _confidence(live)


# ------------------------------------------------------------- severity
def severity_for(
    *,
    verdict: str,
    endpoint: Any,
    is_production: bool,
    availability_pct: float | None,
    days_to_expiry: int | None,
    latency_ratio: float | None,
    application_down: bool,
) -> str:
    """Classify how much attention this deserves.

    Production weighs heavily: the same 502 is a different problem on a
    staging host than on the one customers are using. So does blast radius -
    an application whose endpoints are all down is an outage, whereas one
    failing endpoint among many is a fault.
    """
    down = verdict not in (
        "healthy",
        "recovered_since_last_check",
        "deployment_in_progress",
    )

    # A total outage of a production application is the top of the scale.
    if down and is_production and application_down:
        return DiagnosisSeverity.CRITICAL.value
    if down and is_production and verdict in (
        "dns_failure",
        "connection_refused",
        "connection_timeout",
        "upstream_unavailable",
        "application_error",
        "cert_expired",
        "http_no_response",
    ):
        return DiagnosisSeverity.CRITICAL.value

    if verdict == "cert_expired":
        return DiagnosisSeverity.HIGH.value
    if down and verdict in (
        "dns_failure",
        "connection_refused",
        "connection_timeout",
        "upstream_unavailable",
        "application_error",
        "http_no_response",
        "tls_failure",
    ):
        return DiagnosisSeverity.HIGH.value

    # Intermittent is deliberately rated high: it is harder to catch than a
    # clean outage and is usually ignored until it becomes one.
    if availability_pct is not None and availability_pct < 95:
        return DiagnosisSeverity.HIGH.value

    if days_to_expiry is not None and 0 <= days_to_expiry <= 7:
        return DiagnosisSeverity.MEDIUM.value
    if latency_ratio is not None and latency_ratio >= 3:
        return DiagnosisSeverity.MEDIUM.value
    if verdict in (
        "auth_required",
        "wrong_path",
        "likely_wrong_path_or_expectation",
        "http_status_mismatch",
        "cert_hostname_mismatch",
        "cert_chain_incomplete",
    ):
        return DiagnosisSeverity.MEDIUM.value

    if verdict in ("unexpected_redirect", "cert_self_signed"):
        return DiagnosisSeverity.LOW.value
    if days_to_expiry is not None and days_to_expiry <= 30:
        return DiagnosisSeverity.LOW.value
    if down:
        return DiagnosisSeverity.MEDIUM.value
    return DiagnosisSeverity.INFO.value


# -------------------------------------------------------- not observable
def blind_spots(has_change_data: bool) -> list[Evidence]:
    """What InfraSight cannot see, stated plainly.

    This exists because the most damaging thing a diagnostic tool can do is
    sound authoritative about something it never measured. "Pod is crashing"
    from a tool with no cluster access is a guess wearing a fact's clothing,
    and an engineer who trusts it loses an hour.
    """
    items = [
        Evidence(
            label="Container / pod state",
            value="Not visible from InfraSight",
            kind=EvidenceKind.UNKNOWN.value,
            detail=(
                "Restart counts, CrashLoopBackOff, exit codes and readiness "
                "probes are not observable from an outside HTTP check. If the "
                "service runs on Kubernetes or Docker, check there directly - "
                "the commands below are a starting point."
            ),
        ),
        Evidence(
            label="Host resources",
            value="Not visible from InfraSight",
            kind=EvidenceKind.UNKNOWN.value,
            detail=(
                "CPU, memory, disk and load on the monitored host are not "
                "collected. A slow response is consistent with saturation but "
                "does not demonstrate it."
            ),
        ),
        Evidence(
            label="Application logs",
            value="Not visible from InfraSight",
            kind=EvidenceKind.UNKNOWN.value,
            detail=(
                "Only the status line, timings and headers are recorded - "
                "response bodies are deliberately never stored. The "
                "application's own logs are the authority on why it returned "
                "what it did."
            ),
        ),
        Evidence(
            label="Upstream dependencies",
            value="Not modelled",
            kind=EvidenceKind.UNKNOWN.value,
            detail=(
                "InfraSight has no dependency graph, so it cannot tell you "
                "that this service's database is down. If the dependency is "
                "itself a monitored endpoint, check its status alongside this "
                "one."
            ),
        ),
    ]
    if not has_change_data:
        items.append(
            Evidence(
                label="Recent deployments",
                value="No change record found",
                kind=EvidenceKind.UNKNOWN.value,
                detail=(
                    "Nothing in Change Management covers this endpoint or "
                    "application near the failure. If the deployment was not "
                    "recorded there, the correlation below cannot see it."
                ),
            )
        )
    return items


# ------------------------------------------------------------- commands
def diagnostic_commands(endpoint: Any, verdict: str) -> list[dict[str, str]]:
    """A short, relevant, read-only command list.

    Every command here is safe: it reads state and changes nothing. Twenty
    unrelated commands would be noise, so this returns only what the verdict
    justifies - and the container commands are explicitly conditional, because
    InfraSight does not know whether the service runs in one.
    """
    host = endpoint.hostname
    port = endpoint.port
    url = endpoint.url
    commands: list[dict[str, str]] = []

    def add(command: str, note: str) -> None:
        commands.append({"command": command, "note": note, "risk": ActionRisk.SAFE.value})

    if verdict == "dns_failure":
        add(f"dig +short {host}", "Does the name resolve at all, and to what?")
        add(f"dig +trace {host}", "Follow the delegation to find where resolution breaks.")
        add(
            f"docker compose exec worker getent hosts {host}",
            "Resolve from inside the worker container - an internal-only name "
            "may resolve for you but not for InfraSight.",
        )
        return commands

    if verdict in ("connection_refused", "connection_timeout", "partial_backend"):
        add(f"dig +short {host}", "Confirm which addresses are being tried.")
        add(
            f"nc -vz -w 5 {host} {port}",
            "Is anything accepting connections on that port?",
        )
        add(
            f"curl -sS -o /dev/null -w '%{{http_code}} in %{{time_total}}s\\n' --max-time 10 {url}",
            "Reproduce the request the monitor makes.",
        )
        return commands

    if verdict.startswith("cert_") or verdict == "tls_failure":
        add(
            f"openssl s_client -connect {host}:{port} -servername {host} "
            f"</dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates",
            "Subject, issuer and validity window of the certificate actually served.",
        )
        add(
            f"openssl s_client -connect {host}:{port} -servername {host} -showcerts </dev/null",
            "The full chain as served - a missing intermediate shows up here.",
        )
        add(
            f"curl -vI --max-time 10 {url}",
            "Verify the handshake end to end from a normal client.",
        )
        return commands

    # HTTP-layer and everything else.
    add(
        f"curl -sS -o /dev/null -w 'HTTP %{{http_code}} in %{{time_total}}s\\n' --max-time 15 {url}",
        "Current status and total time, without a body.",
    )
    add(f"curl -sSI --max-time 15 {url}", "Response headers - often name the upstream.")
    if verdict in ("upstream_unavailable", "application_error", "http_no_response"):
        add(
            "kubectl get pods -n <namespace> -l app=<label>",
            "IF this runs on Kubernetes. InfraSight cannot see the cluster; "
            "this is a suggestion, not an observation.",
        )
        add(
            "kubectl logs <pod> -n <namespace> --tail=200 --previous",
            "IF on Kubernetes. --previous shows the container that died, which "
            "is where a crash loop explains itself.",
        )
        add(
            "docker ps -a && docker logs --tail 200 <container>",
            "IF this runs in Docker on a host you control.",
        )
    return commands


# --------------------------------------------------------- verification
def verification_plan(
    endpoint: Any, verdict: str, *, baseline_ms: float | None
) -> list[str]:
    """How to tell the fix actually worked.

    "Apply this fix" without a verification step is how an incident gets
    closed twice. The success criteria are concrete and, where the data
    allows, numeric.
    """
    expected = endpoint.expected_status_codes or "200"
    checks: list[str] = []

    if verdict == "dns_failure":
        checks.append(f"{endpoint.hostname} resolves to the intended address")
    if verdict in ("connection_refused", "connection_timeout", "partial_backend"):
        checks.append(f"Every resolved address accepts a connection on port {endpoint.port}")
    if verdict.startswith("cert_") or verdict == "tls_failure":
        checks.append("The TLS handshake completes with chain verification enabled")
        checks.append("Certificate status returns to Valid on the SSL page")

    checks.append(f"The endpoint returns HTTP {expected}")
    if baseline_ms:
        checks.append(
            f"Response time returns to roughly {int(baseline_ms)} ms "
            "(its own 24-hour baseline)"
        )
    checks.append("Any open incident for this endpoint closes on its own")
    checks.append(
        f"{max(3, (endpoint.failure_threshold or 3))} consecutive scheduled "
        "checks pass without a new failure"
    )
    checks.append("Re-run Diagnose and confirm the verdict is Healthy")
    return checks


# --------------------------------------------------------------- helpers
def is_total_application_outage(app_summary: dict[str, Any] | None) -> bool:
    """Every monitored endpoint of this application is down.

    That is an outage of the application rather than a fault in one of its
    endpoints, and it changes both the severity and who needs waking.
    """
    if not app_summary:
        return False
    total = app_summary.get("total") or 0
    down = app_summary.get("down") or 0
    return total >= 2 and down >= total


def endpoint_is_down(endpoint: Any) -> bool:
    return endpoint.current_status in (
        EndpointStatus.DOWN.value,
        EndpointStatus.DEGRADED.value,
    )
