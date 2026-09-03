"""The Diagnose reasoning layer.

The scoring model and the honesty rules are what is worth testing here. A
wrong probe is a bug; a diagnosis that sounds certain on one weak signal, or
that claims to know something it never measured, is worse - it sends an
engineer down the wrong path while sounding authoritative.
"""

from __future__ import annotations


from app.core.enums import ActionRisk, Confidence, DiagnosisSeverity, EvidenceKind
from app.services.diagnosis_reasoning import (
    CIRCUMSTANTIAL,
    DIRECT,
    STRONG,
    SUPPORTING,
    Candidate,
    Evidence,
    blind_spots,
    diagnostic_commands,
    rank,
    severity_for,
    verification_plan,
)


def _candidate(cause: str, points: list[int]) -> Candidate:
    candidate = Candidate(cause=cause, label=cause, explanation=cause)
    for index, weight in enumerate(points):
        candidate.add(weight, f"signal {index}")
    return candidate


class TestRanking:
    def test_orders_by_evidence_weight(self):
        ranked, _ = rank([
            _candidate("weak", [CIRCUMSTANTIAL]),
            _candidate("strong", [DIRECT, SUPPORTING]),
            _candidate("middling", [STRONG]),
        ])
        assert [c["cause"] for c in ranked] == ["strong", "middling", "weak"]

    def test_shares_sum_to_one(self):
        ranked, _ = rank([
            _candidate("a", [DIRECT]),
            _candidate("b", [STRONG]),
            _candidate("c", [SUPPORTING]),
        ])
        assert abs(sum(c["share"] for c in ranked) - 1.0) < 0.01

    def test_only_the_leader_is_most_likely(self):
        ranked, _ = rank([
            _candidate("a", [DIRECT, DIRECT]),
            _candidate("b", [STRONG]),
            _candidate("c", [CIRCUMSTANTIAL]),
        ])
        assert ranked[0]["band"] == "most_likely"
        assert [c["band"] for c in ranked[1:]] == ["possible", "less_likely"]

    def test_unscored_candidates_are_dropped(self):
        ranked, _ = rank([_candidate("real", [DIRECT]), _candidate("empty", [])])
        assert [c["cause"] for c in ranked] == ["real"]

    def test_nothing_scored_is_low_confidence(self):
        ranked, confidence = rank([])
        assert ranked == []
        assert confidence == Confidence.LOW.value


class TestConfidence:
    """Confidence has to come from the evidence, not from how the sentence reads."""

    def test_one_signal_alone_is_never_high(self):
        """A single observation with no competition is still one observation.

        This is the guard against the most damaging failure mode: sounding
        certain because nothing contradicted a lone probe.
        """
        _, confidence = rank([_candidate("solo", [DIRECT])])
        assert confidence == Confidence.MEDIUM.value

    def test_several_independent_signals_are_high(self):
        _, confidence = rank([
            _candidate("leader", [DIRECT, SUPPORTING, SUPPORTING]),
            _candidate("other", [CIRCUMSTANTIAL]),
        ])
        assert confidence == Confidence.HIGH.value

    def test_a_close_second_place_lowers_confidence(self):
        """Two explanations fitting equally well is exactly when not to be sure."""
        _, confidence = rank([
            _candidate("a", [DIRECT, SUPPORTING]),
            _candidate("b", [DIRECT, SUPPORTING]),
        ])
        assert confidence == Confidence.MEDIUM.value

    def test_only_circumstantial_evidence_is_low(self):
        _, confidence = rank([_candidate("hunch", [CIRCUMSTANTIAL, CIRCUMSTANTIAL])])
        assert confidence == Confidence.LOW.value


class TestSeverity:
    def _severity(self, **overrides):
        payload = {
            "verdict": "upstream_unavailable",
            "endpoint": None,
            "is_production": False,
            "availability_pct": None,
            "days_to_expiry": None,
            "latency_ratio": None,
            "application_down": False,
        }
        payload.update(overrides)
        return severity_for(**payload)

    def test_production_outage_is_critical(self):
        assert self._severity(is_production=True) == DiagnosisSeverity.CRITICAL.value

    def test_the_same_failure_in_staging_is_high(self):
        """Environment is not decoration - it is most of the severity."""
        assert self._severity(is_production=False) == DiagnosisSeverity.HIGH.value

    def test_a_whole_application_down_in_production_is_critical(self):
        assert (
            self._severity(
                verdict="http_status_mismatch",
                is_production=True,
                application_down=True,
            )
            == DiagnosisSeverity.CRITICAL.value
        )

    def test_intermittent_is_rated_high(self):
        """Harder to catch than a clean outage, and usually ignored until it
        becomes one."""
        assert (
            self._severity(verdict="healthy", availability_pct=70.0)
            == DiagnosisSeverity.HIGH.value
        )

    def test_healthy_is_info(self):
        assert (
            self._severity(verdict="healthy", availability_pct=100.0)
            == DiagnosisSeverity.INFO.value
        )

    def test_certificate_expiry_scales_with_time_left(self):
        assert (
            self._severity(verdict="healthy", days_to_expiry=3)
            == DiagnosisSeverity.MEDIUM.value
        )
        assert (
            self._severity(verdict="healthy", days_to_expiry=25)
            == DiagnosisSeverity.LOW.value
        )
        assert (
            self._severity(verdict="healthy", days_to_expiry=200)
            == DiagnosisSeverity.INFO.value
        )

    def test_a_deployment_in_progress_is_not_a_fault(self):
        assert (
            self._severity(verdict="deployment_in_progress", is_production=True)
            == DiagnosisSeverity.INFO.value
        )


class TestHonesty:
    """The rules that stop a diagnostic tool from inventing facts."""

    def test_unobservable_things_are_listed_not_guessed(self):
        items = blind_spots(has_change_data=True)
        assert items, "the blind-spot list must never be empty"
        assert all(item.kind == EvidenceKind.UNKNOWN.value for item in items)

        labels = " ".join(item.label.lower() for item in items)
        for topic in ("container", "resource", "log", "dependenc"):
            assert topic in labels, f"{topic} must be declared unobservable"

    def test_no_blind_spot_asserts_a_state(self):
        """A claim like 'Pod is crashing', from a tool with no cluster access,
        is a guess wearing a fact's clothing. Blind spots must state absence,
        never a condition."""
        for item in blind_spots(has_change_data=True):
            assert "not visible" in item.value.lower() or "not modelled" in item.value.lower()

    def test_missing_change_data_is_declared(self):
        with_changes = blind_spots(has_change_data=True)
        without = blind_spots(has_change_data=False)
        assert len(without) == len(with_changes) + 1
        assert any("deployment" in item.label.lower() for item in without)


class TestCommands:
    class _Endpoint:
        hostname = "api.example.com"
        port = 443
        url = "https://api.example.com/health"
        expected_status_codes = "200"
        failure_threshold = 3
        timeout_seconds = 10

    def test_every_suggested_command_is_read_only(self):
        """Diagnose suggests; it never proposes a destructive command inline."""
        for verdict in (
            "dns_failure",
            "connection_refused",
            "cert_expired",
            "upstream_unavailable",
            "application_error",
        ):
            for entry in diagnostic_commands(self._Endpoint(), verdict):
                assert entry["risk"] == ActionRisk.SAFE.value
                lowered = entry["command"].lower()
                for destructive in ("rm ", "delete", "drop ", "restart", "kill", "scale"):
                    assert destructive not in lowered, entry["command"]

    def test_commands_are_relevant_to_the_verdict(self):
        dns = " ".join(c["command"] for c in diagnostic_commands(self._Endpoint(), "dns_failure"))
        assert "dig" in dns
        assert "openssl" not in dns

        tls = " ".join(c["command"] for c in diagnostic_commands(self._Endpoint(), "cert_expired"))
        assert "openssl" in tls

    def test_container_commands_are_marked_conditional(self):
        """CertMonitor cannot see whether a container is involved at all, so a
        kubectl suggestion must not read as an observation."""
        entries = diagnostic_commands(self._Endpoint(), "upstream_unavailable")
        for entry in entries:
            if "kubectl" in entry["command"] or "docker" in entry["command"]:
                assert entry["note"].startswith("IF"), entry["note"]

    def test_the_list_stays_short(self):
        for verdict in ("dns_failure", "cert_expired", "application_error"):
            assert len(diagnostic_commands(self._Endpoint(), verdict)) <= 6


class TestVerificationPlan:
    class _Endpoint:
        hostname = "api.example.com"
        port = 443
        url = "https://api.example.com/health"
        expected_status_codes = "200"
        failure_threshold = 4
        timeout_seconds = 10

    def test_includes_a_numeric_target_when_a_baseline_exists(self):
        plan = verification_plan(self._Endpoint(), "application_error", baseline_ms=180.0)
        assert any("180 ms" in item for item in plan)

    def test_requires_sustained_recovery_not_one_passing_check(self):
        plan = verification_plan(self._Endpoint(), "application_error", baseline_ms=None)
        assert any("consecutive" in item for item in plan)
        assert any("Re-run Diagnose" in item for item in plan)

    def test_layer_specific_checks_are_added(self):
        dns = verification_plan(self._Endpoint(), "dns_failure", baseline_ms=None)
        assert any("resolves" in item for item in dns)

        tls = verification_plan(self._Endpoint(), "cert_expired", baseline_ms=None)
        assert any("handshake" in item for item in tls)


class TestEvidenceStructure:
    def test_evidence_defaults_to_observed(self):
        assert Evidence(label="x", value="y").kind == EvidenceKind.OBSERVED.value

    def test_evidence_serialises_its_kind(self):
        payload = Evidence(
            label="Container state", value="Not visible",
            kind=EvidenceKind.UNKNOWN.value,
        ).as_dict()
        assert payload["kind"] == EvidenceKind.UNKNOWN.value
