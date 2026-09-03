"""Endpoint check execution.

HTTP responses are stubbed with respx so the outcomes are deterministic and the
suite needs no network. The DNS phase is patched because the hostnames used
here do not resolve.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.enums import CheckStatus, FailureReason
from app.monitoring.checker import CheckTarget, resolve_host, run_check


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """Resolve every hostname to a private address the checker will accept."""

    async def _resolve(hostname, port, *, timeout):
        return "10.20.30.40", 1.5, None

    monkeypatch.setattr("app.monitoring.checker.resolve_host", _resolve)
    return _resolve


def target(**overrides) -> CheckTarget:
    base = {
        "url": "https://api.example.com/health",
        "hostname": "api.example.com",
        "port": 443,
        "protocol": "https",
        "timeout_seconds": 5,
        "expected_status_codes": [200],
        # Certificate inspection needs a real TLS handshake; the HTTP-outcome
        # tests switch it off and the SSL tests cover it separately.
        "ssl_monitoring_enabled": False,
    }
    base.update(overrides)
    return CheckTarget(**base)


class TestHealthyEndpoint:
    @respx.mock
    async def test_reports_up_with_timings(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.UP.value
        assert outcome.http_status_code == 200
        assert outcome.failure_reason == FailureReason.NONE.value
        assert outcome.error_message is None
        assert outcome.response_time_ms is not None and outcome.response_time_ms >= 0
        assert outcome.dns_time_ms == 1.5
        assert outcome.resolved_ip == "10.20.30.40"
        assert outcome.is_up is True

    @respx.mock
    async def test_records_content_length_but_not_the_body(self):
        """Monitoring must not become a data-exfiltration path."""
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200, text="secret-payload-contents")
        )

        outcome = await run_check(target())

        assert outcome.content_length == len(b"secret-payload-contents")
        # The outcome object has no field that could carry the body.
        assert not hasattr(outcome, "body")
        assert "secret-payload" not in str(outcome.__dict__)

    @respx.mock
    async def test_captures_only_interesting_response_headers(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(
                200,
                headers={
                    "Server": "nginx",
                    "Set-Cookie": "session=super-secret",
                    "X-Request-ID": "abc123",
                },
            )
        )

        outcome = await run_check(target())

        assert outcome.response_headers.get("server") == "nginx"
        assert outcome.response_headers.get("x-request-id") == "abc123"
        # A session cookie is never stored.
        assert "set-cookie" not in outcome.response_headers

    @respx.mock
    async def test_accepts_any_configured_status_code(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(204)
        )
        outcome = await run_check(target(expected_status_codes=[200, 204]))
        assert outcome.status == CheckStatus.UP.value


class TestFailures:
    @respx.mock
    async def test_unexpected_status_is_down(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(500)
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.HTTP_STATUS_MISMATCH.value
        assert outcome.http_status_code == 500
        assert "500" in outcome.error_message

    @respx.mock
    async def test_connect_timeout(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.CONNECTION_TIMEOUT.value
        assert outcome.error_message == "Connection timeout"

    @respx.mock
    async def test_read_timeout(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        outcome = await run_check(target())
        assert outcome.failure_reason == FailureReason.READ_TIMEOUT.value

    @respx.mock
    async def test_connection_refused(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.CONNECTION_REFUSED.value

    @respx.mock
    async def test_tls_error_is_classified_as_such(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.ConnectError(
                "certificate verify failed: unable to get local issuer certificate"
            )
        )

        outcome = await run_check(target())

        assert outcome.failure_reason == FailureReason.TLS_ERROR.value

    @respx.mock
    async def test_too_many_redirects(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.TooManyRedirects("loop")
        )
        outcome = await run_check(target())
        assert outcome.failure_reason == FailureReason.TOO_MANY_REDIRECTS.value

    async def test_dns_failure_short_circuits(self, monkeypatch):
        async def _fail(hostname, port, *, timeout):
            return None, 12.0, "DNS resolution failed: Name or service not known"

        monkeypatch.setattr("app.monitoring.checker.resolve_host", _fail)

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.DNS_FAILURE.value
        assert outcome.dns_time_ms == 12.0
        # No connection was attempted, so there is no HTTP status.
        assert outcome.http_status_code is None

    async def test_loopback_is_refused_when_not_permitted(self, monkeypatch):
        async def _loopback(hostname, port, *, timeout):
            return "127.0.0.1", 0.4, None

        monkeypatch.setattr("app.monitoring.checker.resolve_host", _loopback)
        monkeypatch.setattr(
            "app.monitoring.validators.settings.ALLOW_LOOPBACK_TARGETS", False
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.BLOCKED_TARGET.value


class TestDegraded:
    @respx.mock
    async def test_slow_but_successful_response_is_degraded(self):
        """A slow success is not a failure, but it is not healthy either."""
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        # A 0 ms threshold guarantees the observed duration exceeds it.
        outcome = await run_check(target(response_time_threshold_ms=0.0001))

        assert outcome.status == CheckStatus.DEGRADED.value
        assert outcome.failure_reason == FailureReason.SLOW_RESPONSE.value
        # Degraded still counts as reachable.
        assert outcome.is_up is True

    @respx.mock
    async def test_no_threshold_means_never_degraded(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )
        outcome = await run_check(target(response_time_threshold_ms=None))
        assert outcome.status == CheckStatus.UP.value


class TestBodyMatching:
    @respx.mock
    async def test_expected_content_present(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200, text='{"status":"ok","version":3}')
        )
        outcome = await run_check(target(expected_body_substring='"status":"ok"'))
        assert outcome.status == CheckStatus.UP.value

    @respx.mock
    async def test_expected_content_missing_fails_the_check(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200, text='{"status":"degraded"}')
        )

        outcome = await run_check(target(expected_body_substring='"status":"ok"'))

        assert outcome.status == CheckStatus.DOWN.value
        assert "expected content" in outcome.error_message.lower()


class TestAuthentication:
    @respx.mock
    async def test_bearer_token_is_sent(self):
        route = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        await run_check(target(auth_type="bearer", auth_secret="tok-12345"))

        assert route.calls[0].request.headers["authorization"] == "Bearer tok-12345"

    @respx.mock
    async def test_basic_auth_is_encoded(self):
        route = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        await run_check(
            target(auth_type="basic", auth_username="probe", auth_secret="pw")
        )

        header = route.calls[0].request.headers["authorization"]
        assert header.startswith("Basic ")
        import base64

        assert base64.b64decode(header.split(" ", 1)[1]).decode() == "probe:pw"

    @respx.mock
    async def test_custom_header_auth(self):
        route = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        await run_check(
            target(auth_type="header", auth_header_name="X-Api-Key", auth_secret="k1")
        )

        assert route.calls[0].request.headers["x-api-key"] == "k1"

    @respx.mock
    async def test_custom_headers_are_applied(self):
        route = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        await run_check(target(custom_headers={"X-Api-Version": "2"}))

        assert route.calls[0].request.headers["x-api-version"] == "2"


class TestMethods:
    @respx.mock
    async def test_post_with_a_body(self):
        route = respx.post("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(
            target(http_method="POST", request_body='{"ping":true}')
        )

        assert outcome.status == CheckStatus.UP.value
        assert route.calls[0].request.content == b'{"ping":true}'


class TestRedirects:
    @respx.mock
    async def test_redirect_chain_is_recorded(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://api.example.com/v2/health"}
            )
        )
        respx.get("https://api.example.com/v2/health").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target(follow_redirects=True))

        assert outcome.status == CheckStatus.UP.value
        assert outcome.redirect_count == 1
        assert outcome.final_url.endswith("/v2/health")
        assert outcome.redirect_chain[0]["status_code"] == 302

    @respx.mock
    async def test_redirect_without_following_uses_the_status(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(
                301, headers={"Location": "https://elsewhere.example.com/"}
            )
        )

        outcome = await run_check(
            target(follow_redirects=False, expected_status_codes=[301])
        )

        assert outcome.status == CheckStatus.UP.value
        assert outcome.redirect_count == 0


class TestRealDnsResolution:
    async def test_resolve_host_reports_an_error_for_a_bogus_name(self):
        """Exercises the real resolver; no outbound connection is made."""
        ip, elapsed, error = await resolve_host(
            "this-name-should-not-resolve.invalid", 443, timeout=3
        )
        assert ip is None
        assert error is not None
        assert elapsed >= 0


class TestHealthPathDiscovery:
    """Endpoints in one fleet rarely agree on where health lives.

    Discovery only ever runs when the configured path is *definitively*
    absent. Every other failure must be reported as itself - turning a 500 or
    a timeout into a pass by probing elsewhere would be worse than the 404.
    """

    CANDIDATES = ["/healthz", "/ready", "/actuator/health"]

    @respx.mock
    async def test_adopts_the_first_path_that_answers(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://api.example.com/ready").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.status == CheckStatus.UP.value
        assert outcome.resolved_path == "/ready"
        assert outcome.http_status_code == 200
        # The trail is recorded so the adoption is auditable.
        assert [p["path"] for p in outcome.path_probes] == [
            "/health",
            "/healthz",
            "/ready",
        ]
        assert outcome.path_probes[-1]["adopted"] is True

    @respx.mock
    async def test_stays_down_when_no_path_answers(self):
        """Not finding a health endpoint is not evidence of health."""
        for path in ("/health", "/healthz", "/ready", "/actuator/health"):
            respx.get(f"https://api.example.com{path}").mock(
                return_value=httpx.Response(404)
            )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.resolved_path is None
        assert outcome.http_status_code == 404
        assert len(outcome.path_probes) == 4

    @respx.mock
    async def test_a_server_error_is_never_probed_around(self):
        """500 means the application is there and broken. Searching for a
        path that happens to return 200 would report a broken service as up."""
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(500)
        )
        healthz = respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.http_status_code == 500
        assert outcome.resolved_path is None
        assert not healthz.called

    @respx.mock
    async def test_an_auth_gated_path_is_never_probed_around(self):
        """401/403 means the path exists - it is simply protected."""
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(401)
        )
        healthz = respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.http_status_code == 401
        assert outcome.resolved_path is None
        assert not healthz.called

    @respx.mock
    async def test_a_timeout_is_never_probed_around(self):
        respx.get("https://api.example.com/health").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        healthz = respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.failure_reason == FailureReason.CONNECTION_TIMEOUT.value
        assert outcome.resolved_path is None
        assert not healthz.called

    @respx.mock
    async def test_discovery_is_off_by_default(self):
        """No candidates configured means exactly one request, as before."""
        configured = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(404)
        )
        healthz = respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target())

        assert outcome.status == CheckStatus.DOWN.value
        assert outcome.resolved_path is None
        assert configured.call_count == 1
        assert not healthz.called

    @respx.mock
    async def test_a_healthy_endpoint_never_probes_alternatives(self):
        """The common case must cost exactly one request."""
        configured = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(200)
        )
        healthz = respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(target(health_path_candidates=self.CANDIDATES))

        assert outcome.status == CheckStatus.UP.value
        assert outcome.resolved_path is None
        assert configured.call_count == 1
        assert not healthz.called

    @respx.mock
    async def test_the_query_string_is_preserved(self):
        respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(404)
        )
        route = respx.get("https://api.example.com/healthz", params={"verbose": "1"}).mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(
            target(
                url="https://api.example.com/health?verbose=1",
                health_path_candidates=["/healthz"],
            )
        )

        assert outcome.resolved_path == "/healthz"
        assert route.called

    @respx.mock
    async def test_the_configured_path_is_not_retried_as_a_candidate(self):
        configured = respx.get("https://api.example.com/health").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://api.example.com/healthz").mock(
            return_value=httpx.Response(200)
        )

        outcome = await run_check(
            target(health_path_candidates=["/health", "/health/", "/healthz"])
        )

        assert outcome.resolved_path == "/healthz"
        assert configured.call_count == 1
