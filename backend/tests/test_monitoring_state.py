"""Status transitions, incident lifecycle and alert generation.

These tests exercise the requirement that a run of consecutive failures is ONE
incident, not one per failed check, and that recovery closes it with a computed
downtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.enums import (
    AlertType,
    CheckStatus,
    EndpointStatus,
    FailureReason,
    IncidentStatus,
    SslStatus,
)
from app.models.alert import Alert
from app.models.endpoint import Endpoint
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate
from app.monitoring.checker import CheckOutcome
from app.monitoring.ssl_inspect import CertificateInfo
from app.services import monitoring_service


def failure(
    *,
    reason: str = FailureReason.CONNECTION_TIMEOUT.value,
    message: str = "Connection timeout",
    at: datetime | None = None,
) -> CheckOutcome:
    outcome = CheckOutcome()
    outcome.status = CheckStatus.DOWN.value
    outcome.failure_reason = reason
    outcome.error_message = message
    outcome.checked_at = at or datetime.now(timezone.utc)
    return outcome


def success(
    *,
    status_code: int = 200,
    response_ms: float = 120.0,
    at: datetime | None = None,
    degraded: bool = False,
) -> CheckOutcome:
    outcome = CheckOutcome()
    outcome.status = (
        CheckStatus.DEGRADED.value if degraded else CheckStatus.UP.value
    )
    outcome.failure_reason = (
        FailureReason.SLOW_RESPONSE.value if degraded else FailureReason.NONE.value
    )
    outcome.http_status_code = status_code
    outcome.response_time_ms = response_ms
    outcome.total_time_ms = response_ms
    outcome.checked_at = at or datetime.now(timezone.utc)
    if degraded:
        outcome.error_message = "Response time exceeded the threshold"
    return outcome


# Notifications are not dispatched in these tests: delivery is covered
# separately, and an outbound webhook attempt would slow every case down.
async def record(session, endpoint, outcome, config, **kwargs):
    kwargs.setdefault("dispatch_notifications", False)
    return await monitoring_service.record_check_result(
        session, endpoint, outcome, config=config, **kwargs
    )


class TestResultPersistence:
    async def test_a_check_writes_one_result_row(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        await record(session, endpoint, success(), runtime_config)
        await session.commit()

        total = (
            await session.execute(select(func.count(MonitoringResult.id)))
        ).scalar()
        assert total == 1

    async def test_live_state_is_updated(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        await record(session, endpoint, success(status_code=201, response_ms=88.5), runtime_config)
        await session.commit()

        assert endpoint.current_status == EndpointStatus.UP.value
        assert endpoint.last_status_code == 201
        assert endpoint.last_response_time_ms == 88.5
        assert endpoint.last_checked_at is not None
        assert endpoint.total_checks == 1
        assert endpoint.total_failures == 0
        assert endpoint.consecutive_successes == 1
        assert endpoint.consecutive_failures == 0

    async def test_failure_counters(self, session, endpoint_factory, runtime_config):
        endpoint = await endpoint_factory()
        await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        assert endpoint.current_status == EndpointStatus.DOWN.value
        assert endpoint.consecutive_failures == 1
        assert endpoint.consecutive_successes == 0
        assert endpoint.total_failures == 1
        assert endpoint.last_error == "Connection timeout"


class TestIncidentLifecycle:
    async def test_no_incident_before_the_threshold(
        self, session, endpoint_factory, runtime_config
    ):
        """A single blip must not open an incident."""
        endpoint = await endpoint_factory(failure_threshold=3)

        for _ in range(2):
            await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        count = (await session.execute(select(func.count(Incident.id)))).scalar()
        assert count == 0
        assert endpoint.current_status == EndpointStatus.DOWN.value

    async def test_incident_opens_at_the_threshold(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=3)

        for _ in range(3):
            recorded = await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        assert recorded.incident_opened is not None
        incidents = (await session.execute(select(Incident))).scalars().all()
        assert len(incidents) == 1
        assert incidents[0].status == IncidentStatus.OPEN.value
        assert incidents[0].reason == FailureReason.CONNECTION_TIMEOUT.value

    async def test_many_consecutive_failures_are_one_incident(
        self, session, endpoint_factory, runtime_config
    ):
        """Check 1..8 all DOWN must produce exactly one incident."""
        endpoint = await endpoint_factory(failure_threshold=3)

        for _ in range(8):
            await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        incidents = (await session.execute(select(Incident))).scalars().all()
        assert len(incidents) == 1
        # Every failure after the threshold is counted on the same incident.
        assert incidents[0].failed_check_count == 6
        assert endpoint.consecutive_failures == 8

    async def test_recovery_closes_the_incident_and_computes_downtime(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=2)
        started = datetime.now(timezone.utc) - timedelta(minutes=5)

        await record(session, endpoint, failure(at=started), runtime_config)
        await record(
            session, endpoint, failure(at=started + timedelta(seconds=30)), runtime_config
        )
        await session.commit()

        recovered_at = started + timedelta(minutes=5)
        recorded = await record(
            session, endpoint, success(at=recovered_at), runtime_config
        )
        await session.commit()

        assert recorded.incident_closed is not None
        incident = recorded.incident_closed
        assert incident.status == IncidentStatus.RESOLVED.value
        assert incident.resolved_at == recovered_at
        # Started at the first failure, resolved on recovery: ~5 minutes.
        assert 290 <= incident.duration_seconds <= 310
        assert incident.recovery_status_code == 200
        assert endpoint.current_status == EndpointStatus.UP.value

    async def test_a_new_outage_opens_a_second_incident(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=1)

        await record(session, endpoint, failure(), runtime_config)
        await record(session, endpoint, success(), runtime_config)
        await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        incidents = (
            await session.execute(select(Incident).order_by(Incident.started_at))
        ).scalars().all()
        assert len(incidents) == 2
        assert incidents[0].status == IncidentStatus.RESOLVED.value
        assert incidents[1].status == IncidentStatus.OPEN.value

    async def test_only_one_incident_may_be_open_per_endpoint(
        self, session, endpoint_factory, runtime_config
    ):
        """Enforced by a partial unique index, not only by application logic."""
        endpoint = await endpoint_factory(failure_threshold=1)
        await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        open_count = (
            await session.execute(
                select(func.count(Incident.id)).where(
                    Incident.endpoint_id == endpoint.id,
                    Incident.status == IncidentStatus.OPEN.value,
                )
            )
        ).scalar()
        assert open_count == 1

        session.add(
            Incident(
                endpoint_id=endpoint.id,
                status=IncidentStatus.OPEN.value,
                started_at=datetime.now(timezone.utc),
                failed_check_count=1,
            )
        )
        with pytest.raises(Exception):
            await session.commit()
        await session.rollback()

    async def test_a_changed_reason_is_appended_to_the_timeline(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=1)

        await record(session, endpoint, failure(), runtime_config)
        await record(
            session,
            endpoint,
            failure(reason=FailureReason.DNS_FAILURE.value, message="DNS gone"),
            runtime_config,
        )
        await session.commit()

        incident = (await session.execute(select(Incident))).scalar_one()
        kinds = [entry["kind"] for entry in incident.timeline]
        assert "opened" in kinds
        assert "reason_changed" in kinds
        assert incident.reason == FailureReason.DNS_FAILURE.value

    async def test_recovery_threshold_can_require_two_successes(
        self, session, endpoint_factory, runtime_config
    ):
        """Guards against a flapping endpoint closing an incident too early."""
        config = {**runtime_config, "recovery_threshold": 2}
        endpoint = await endpoint_factory(failure_threshold=1)

        await record(session, endpoint, failure(), config)
        first = await record(session, endpoint, success(), config)
        assert first.incident_closed is None

        second = await record(session, endpoint, success(), config)
        await session.commit()
        assert second.incident_closed is not None


class TestDegraded:
    async def test_degraded_is_up_but_flagged(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        await record(session, endpoint, success(degraded=True), runtime_config)
        await session.commit()

        assert endpoint.current_status == EndpointStatus.DEGRADED.value
        # Degraded does not count as a failure.
        assert endpoint.total_failures == 0

    async def test_degraded_closes_an_open_incident(
        self, session, endpoint_factory, runtime_config
    ):
        """Slow-but-answering means the outage is over."""
        endpoint = await endpoint_factory(failure_threshold=1)
        await record(session, endpoint, failure(), runtime_config)
        recorded = await record(session, endpoint, success(degraded=True), runtime_config)
        await session.commit()

        assert recorded.incident_closed is not None


class TestAlerts:
    async def test_down_alert_accompanies_a_new_incident(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=1)
        await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        alerts = (await session.execute(select(Alert))).scalars().all()
        types = [alert.alert_type for alert in alerts]
        assert AlertType.ENDPOINT_DOWN.value in types

    async def test_recovery_alert_is_raised(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=1)
        await record(session, endpoint, failure(), runtime_config)
        await record(session, endpoint, success(), runtime_config)
        await session.commit()

        types = [
            alert.alert_type
            for alert in (await session.execute(select(Alert))).scalars().all()
        ]
        assert AlertType.ENDPOINT_RECOVERED.value in types

    async def test_cooldown_suppresses_a_repeat_alert(
        self, session, endpoint_factory, runtime_config
    ):
        """Two separate outages inside the cooldown yield one DOWN alert."""
        config = {**runtime_config, "alert_cooldown_minutes": 60}
        endpoint = await endpoint_factory(failure_threshold=1)

        await record(session, endpoint, failure(), config)
        await record(session, endpoint, success(), config)
        await record(session, endpoint, failure(), config)
        await session.commit()

        down_alerts = (
            await session.execute(
                select(func.count(Alert.id)).where(
                    Alert.alert_type == AlertType.ENDPOINT_DOWN.value
                )
            )
        ).scalar()
        assert down_alerts == 1

    async def test_recovery_alerts_ignore_the_cooldown(
        self, session, endpoint_factory, runtime_config
    ):
        """Suppressing an all-clear is worse than sending one too many."""
        config = {**runtime_config, "alert_cooldown_minutes": 60}
        endpoint = await endpoint_factory(failure_threshold=1)

        for _ in range(2):
            await record(session, endpoint, failure(), config)
            await record(session, endpoint, success(), config)
        await session.commit()

        recovered = (
            await session.execute(
                select(func.count(Alert.id)).where(
                    Alert.alert_type == AlertType.ENDPOINT_RECOVERED.value
                )
            )
        ).scalar()
        assert recovered == 2

    async def test_alerts_disabled_on_the_endpoint_suppresses_everything(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory(failure_threshold=1, alerts_enabled=False)
        await record(session, endpoint, failure(), runtime_config)
        await session.commit()

        count = (await session.execute(select(func.count(Alert.id)))).scalar()
        assert count == 0
        # The incident is still recorded - only notification is suppressed.
        incidents = (await session.execute(select(func.count(Incident.id)))).scalar()
        assert incidents == 1

    async def test_global_switch_suppresses_alerts(
        self, session, endpoint_factory, runtime_config
    ):
        config = {**runtime_config, "alerts_enabled": False}
        endpoint = await endpoint_factory(failure_threshold=1)
        await record(session, endpoint, failure(), config)
        await session.commit()

        count = (await session.execute(select(func.count(Alert.id)))).scalar()
        assert count == 0

    async def test_degraded_raises_a_latency_alert(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        await record(session, endpoint, success(degraded=True), runtime_config)
        await session.commit()

        types = [
            alert.alert_type
            for alert in (await session.execute(select(Alert))).scalars().all()
        ]
        assert AlertType.HIGH_RESPONSE_TIME.value in types


class TestCertificatePersistence:
    def _cert_info(self, *, days=60, fingerprint="AA:BB", status=SslStatus.VALID.value):
        info = CertificateInfo()
        info.fingerprint_sha256 = fingerprint
        info.common_name = "api.example.com"
        info.issuer_common_name = "Example CA"
        info.issuer = "CN=Example CA"
        info.san = ["api.example.com"]
        info.valid_from = datetime.now(timezone.utc) - timedelta(days=30)
        info.valid_to = datetime.now(timezone.utc) + timedelta(days=days)
        info.days_remaining = days
        info.status = status
        info.chain_verified = True
        info.hostname_matches = True
        info.verification_status = "verified"
        return info

    async def test_certificate_row_is_written_and_endpoint_updated(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        outcome = success()
        outcome.certificate = self._cert_info()

        await record(session, endpoint, outcome, runtime_config)
        await session.commit()

        certificates = (await session.execute(select(SslCertificate))).scalars().all()
        assert len(certificates) == 1
        assert certificates[0].is_current is True
        assert endpoint.ssl_status == SslStatus.VALID.value
        assert endpoint.ssl_days_remaining == 60
        assert endpoint.ssl_issuer == "Example CA"

    async def test_same_fingerprint_updates_in_place(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()

        first = success()
        first.certificate = self._cert_info(days=60)
        await record(session, endpoint, first, runtime_config)

        second = success()
        second.certificate = self._cert_info(days=59)
        await record(session, endpoint, second, runtime_config)
        await session.commit()

        certificates = (await session.execute(select(SslCertificate))).scalars().all()
        assert len(certificates) == 1
        assert certificates[0].days_remaining == 59

    async def test_a_rotation_creates_a_second_row(
        self, session, endpoint_factory, runtime_config
    ):
        """Renewals keep a history; only the newest is is_current."""
        endpoint = await endpoint_factory()

        first = success()
        first.certificate = self._cert_info(fingerprint="AA:BB", days=5)
        await record(session, endpoint, first, runtime_config)

        second = success()
        second.certificate = self._cert_info(fingerprint="CC:DD", days=365)
        await record(session, endpoint, second, runtime_config)
        await session.commit()

        certificates = (
            await session.execute(
                select(SslCertificate).order_by(SslCertificate.id)
            )
        ).scalars().all()
        assert len(certificates) == 2
        assert certificates[0].is_current is False
        assert certificates[1].is_current is True
        assert endpoint.ssl_days_remaining == 365

    async def test_expiring_certificate_raises_an_alert(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        outcome = success()
        outcome.certificate = self._cert_info(
            days=5, status=SslStatus.CRITICAL.value
        )

        await record(session, endpoint, outcome, runtime_config)
        await session.commit()

        types = [
            alert.alert_type
            for alert in (await session.execute(select(Alert))).scalars().all()
        ]
        assert AlertType.SSL_EXPIRING.value in types

    async def test_expired_certificate_raises_the_expired_alert(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        outcome = success()
        outcome.certificate = self._cert_info(
            days=-3, status=SslStatus.EXPIRED.value
        )

        await record(session, endpoint, outcome, runtime_config)
        await session.commit()

        types = [
            alert.alert_type
            for alert in (await session.execute(select(Alert))).scalars().all()
        ]
        assert AlertType.SSL_EXPIRED.value in types

    async def test_regrade_recomputes_states_after_a_threshold_change(
        self, session, endpoint_factory, runtime_config
    ):
        endpoint = await endpoint_factory()
        outcome = success()
        outcome.certificate = self._cert_info(days=20, status=SslStatus.VALID.value)
        await record(session, endpoint, outcome, runtime_config)
        await session.commit()

        # Widening the warning window must reclassify the stored certificate
        # immediately, not at the endpoint's next check.
        updated = await monitoring_service.regrade_certificates(
            session, warning_days=60, critical_days=7
        )
        await session.commit()

        assert updated >= 1
        certificate = (await session.execute(select(SslCertificate))).scalar_one()
        assert certificate.status == SslStatus.EXPIRING_SOON.value

        refreshed = (
            await session.execute(select(Endpoint).where(Endpoint.id == endpoint.id))
        ).scalar_one()
        assert refreshed.ssl_status == SslStatus.EXPIRING_SOON.value


class TestScheduling:
    def test_next_check_time_is_jittered_but_bounded(self):
        """Jitter stops a bulk import becoming a thundering herd."""
        times = [monitoring_service.next_check_time(60) for _ in range(20)]
        now = datetime.now(timezone.utc)
        offsets = [(t - now).total_seconds() for t in times]

        assert all(53 <= offset <= 67 for offset in offsets)
        # Not all identical.
        assert len(set(round(o, 3) for o in offsets)) > 1
