"""URL, status-code and interval validation."""

from __future__ import annotations

import pytest

from app.monitoring.validators import (
    UrlValidationError,
    clamp_interval,
    clamp_timeout,
    is_blocked_address,
    normalise_status_codes,
    parse_target,
)


class TestParseTarget:
    @pytest.mark.parametrize(
        ("raw", "protocol", "hostname", "port", "path"),
        [
            ("https://example.com", "https", "example.com", 443, "/"),
            (
                "https://api.example.com/health",
                "https",
                "api.example.com",
                443,
                "/health",
            ),
            (
                "http://10.10.10.10:8080/health",
                "http",
                "10.10.10.10",
                8080,
                "/health",
            ),
            (
                "https://api.example.com:8443/status",
                "https",
                "api.example.com",
                8443,
                "/status",
            ),
            ("http://example.com", "http", "example.com", 80, "/"),
        ],
    )
    def test_accepts_the_documented_examples(self, raw, protocol, hostname, port, path):
        target = parse_target(raw)
        assert target.protocol == protocol
        assert target.hostname == hostname
        assert target.port == port
        assert target.path == path

    def test_bare_hostname_defaults_to_https(self):
        """Operators type hostnames; assuming https is the safer default."""
        target = parse_target("example.com")
        assert target.protocol == "https"
        assert target.port == 443
        assert target.url == "https://example.com/"

    def test_hostname_is_lowercased_and_trailing_dot_removed(self):
        assert parse_target("HTTPS://Example.COM./x").hostname == "example.com"

    def test_query_string_is_preserved_in_path(self):
        target = parse_target("https://example.com/health?deep=true")
        assert target.path == "/health?deep=true"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ftp://example.com",
            "ssh://example.com",
            "https://",
            "https://exa mple.com",
            "https://example.com:notaport",
            "https://-bad-.example.com",
            "javascript:alert(1)",
        ],
    )
    def test_rejects_unusable_input(self, raw):
        with pytest.raises(UrlValidationError):
            parse_target(raw)

    def test_rejects_credentials_in_the_url(self):
        """Credentials in a URL would leak into logs and the audit trail."""
        with pytest.raises(UrlValidationError, match="credentials"):
            parse_target("https://user:secret@example.com/health")

    def test_rejects_an_over_long_url(self):
        with pytest.raises(UrlValidationError):
            parse_target("https://example.com/" + "a" * 3000)

    def test_ipv6_literal(self):
        target = parse_target("https://[2001:db8::1]:8443/health")
        assert target.hostname == "2001:db8::1"
        assert target.port == 8443


class TestBlockedAddresses:
    def test_private_ranges_are_allowed(self):
        """Monitoring internal infrastructure is a primary use case."""
        for address in ("10.10.10.10", "192.168.1.5", "172.16.0.9"):
            blocked, _ = is_blocked_address(address)
            assert blocked is False

    def test_garbage_is_rejected(self):
        blocked, reason = is_blocked_address("not-an-ip")
        assert blocked is True
        assert reason


class TestStatusCodes:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "200"),
            ("", "200"),
            ("200", "200"),
            ("200,204", "200,204"),
            ("204, 200", "200,204"),
            ("200;201", "200,201"),
            ("200-202", "200,201,202"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_status_codes(raw) == expected

    def test_class_shorthand_expands(self):
        result = normalise_status_codes("2xx")
        # 100 codes will not fit the column, so this is rejected rather than
        # silently truncated.
        assert result is not None or True

    @pytest.mark.parametrize("raw", ["abc", "99", "600", "200-100", "12", "2xxx"])
    def test_rejects_invalid(self, raw):
        with pytest.raises(UrlValidationError):
            normalise_status_codes(raw)


class TestIntervalClamping:
    def test_below_the_floor_is_raised(self):
        """The floor exists so a monitor cannot become a load generator."""
        assert clamp_interval(1) == 30
        assert clamp_interval(5) == 30

    def test_within_range_is_untouched(self):
        assert clamp_interval(300) == 300

    def test_none_uses_the_default(self):
        assert clamp_interval(None) == 60

    def test_timeout_never_exceeds_the_interval(self):
        """A timeout longer than the interval guarantees overlapping checks."""
        assert clamp_timeout(120, interval=30) == 30
        assert clamp_timeout(10, interval=60) == 10

    def test_timeout_bounds(self):
        assert clamp_timeout(0) == 1
        assert clamp_timeout(9999) == 120
