"""SSL certificate inspection and expiry classification.

Certificates are generated in-process with `cryptography`, so the valid,
expiring, expired and invalid cases are all real X.509 material rather than
fixtures - the same parsing path a live handshake uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core.enums import SslStatus
from app.monitoring.ssl_inspect import (
    classify_certificate,
    describe_certificate,
    hostname_matches,
)


def make_certificate(
    *,
    common_name: str = "api.example.com",
    san: list[str] | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    issuer_cn: str | None = "Example Issuing CA",
    issuer_org: str = "Example Trust Services",
    key_size: int = 2048,
) -> bytes:
    """Build a DER-encoded certificate. Self-signed when issuer_cn is None."""
    now = datetime.now(timezone.utc)
    not_before = not_before or (now - timedelta(days=30))
    not_after = not_after or (now + timedelta(days=90))

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example Corp"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
        ]
    )
    if issuer_cn is None:
        issuer = subject  # self-signed: subject == issuer
    else:
        issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, issuer_org),
            ]
        )

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before.replace(tzinfo=None))
        .not_valid_after(not_after.replace(tzinfo=None))
    )
    names = san if san is not None else [common_name]
    if names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
            critical=False,
        )

    certificate = builder.sign(private_key=key, algorithm=hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.DER)


def describe(der: bytes, *, hostname="api.example.com", warning=30, critical=7, **kw):
    return describe_certificate(
        der, hostname=hostname, warning_days=warning, critical_days=critical, **kw
    )


class TestValidCertificate:
    def test_full_details_are_extracted(self):
        der = make_certificate()
        info = describe(der, verified=True)

        assert info.status == SslStatus.VALID.value
        assert info.common_name == "api.example.com"
        assert "CN=api.example.com" in info.subject
        assert info.issuer_common_name == "Example Issuing CA"
        assert info.issuer_organization == "Example Trust Services"
        assert info.san == ["api.example.com"]
        assert info.days_remaining is not None and 88 <= info.days_remaining <= 90
        assert info.key_algorithm == "RSA"
        assert info.key_size == 2048
        assert info.signature_algorithm
        assert info.fingerprint_sha256 and len(info.fingerprint_sha256.split(":")) == 32
        assert info.serial_number
        assert info.is_self_signed is False
        assert info.hostname_matches is True
        assert info.chain_verified is True
        assert info.verification_status == "verified"
        assert info.error is None

    def test_valid_from_and_to_are_timezone_aware(self):
        info = describe(make_certificate())
        assert info.valid_from.tzinfo is not None
        assert info.valid_to.tzinfo is not None


class TestExpiryStates:
    def test_expiring_soon(self):
        """Inside the warning window but outside the critical one."""
        der = make_certificate(
            not_after=datetime.now(timezone.utc) + timedelta(days=20)
        )
        info = describe(der, verified=True)
        assert info.status == SslStatus.EXPIRING_SOON.value
        assert 19 <= info.days_remaining <= 20

    def test_critical(self):
        der = make_certificate(
            not_after=datetime.now(timezone.utc) + timedelta(days=5)
        )
        info = describe(der, verified=True)
        assert info.status == SslStatus.CRITICAL.value

    def test_expired(self):
        der = make_certificate(
            not_before=datetime.now(timezone.utc) - timedelta(days=400),
            not_after=datetime.now(timezone.utc) - timedelta(days=10),
        )
        info = describe(der, verified=False)

        assert info.status == SslStatus.EXPIRED.value
        assert info.days_remaining is not None and info.days_remaining < 0
        assert info.verification_status == "expired"

    def test_not_yet_valid(self):
        der = make_certificate(
            not_before=datetime.now(timezone.utc) + timedelta(days=5),
            not_after=datetime.now(timezone.utc) + timedelta(days=100),
        )
        info = describe(der)
        assert info.verification_status == "not_yet_valid"

    def test_day_boundary_rounds_toward_zero(self):
        """23 hours left must read as 0 days, so a '< 7 days' alert is timely."""
        der = make_certificate(
            not_after=datetime.now(timezone.utc) + timedelta(hours=23)
        )
        info = describe(der, verified=True)
        assert info.days_remaining == 0
        assert info.status == SslStatus.CRITICAL.value


class TestInvalidCertificate:
    def test_self_signed_is_detected(self):
        der = make_certificate(issuer_cn=None)
        info = describe(der, verified=False, verification_error="self signed certificate")

        assert info.is_self_signed is True
        assert info.status == SslStatus.INVALID.value
        assert info.chain_verified is False
        assert info.verification_status == "verification_failed"

    def test_hostname_mismatch(self):
        der = make_certificate(common_name="other.example.com", san=["other.example.com"])
        info = describe(der, hostname="api.example.com")

        assert info.hostname_matches is False
        assert info.status == SslStatus.INVALID.value
        assert info.verification_status == "hostname_mismatch"

    def test_unparseable_bytes_do_not_raise(self):
        info = describe(b"this is not a certificate")
        assert info.status == SslStatus.INVALID.value
        assert info.verification_status == "unparseable"
        assert info.error

    def test_expired_takes_priority_over_verification_failure(self):
        der = make_certificate(
            not_before=datetime.now(timezone.utc) - timedelta(days=400),
            not_after=datetime.now(timezone.utc) - timedelta(days=1),
            issuer_cn=None,
        )
        info = describe(der, verified=False)
        assert info.status == SslStatus.EXPIRED.value


class TestWildcard:
    def test_wildcard_is_flagged_and_matches_a_subdomain(self):
        der = make_certificate(common_name="*.example.com", san=["*.example.com"])
        info = describe(der, hostname="api.example.com", verified=True)

        assert info.is_wildcard is True
        assert info.hostname_matches is True
        assert info.status == SslStatus.VALID.value

    def test_wildcard_does_not_match_a_deeper_subdomain(self):
        assert hostname_matches("a.b.example.com", "*.example.com", []) is False

    def test_wildcard_does_not_match_the_bare_domain(self):
        assert hostname_matches("example.com", "*.example.com", []) is False

    def test_san_match_wins_when_cn_differs(self):
        der = make_certificate(
            common_name="primary.example.com",
            san=["primary.example.com", "api.example.com"],
        )
        info = describe(der, hostname="api.example.com")
        assert info.hostname_matches is True
        assert "api.example.com" in info.san


class TestChain:
    def test_chain_entries_are_summarised(self):
        leaf = make_certificate()
        intermediate = make_certificate(common_name="Example Issuing CA", issuer_cn="Example Root")
        root = make_certificate(common_name="Example Root", issuer_cn=None)

        info = describe(leaf, chain_der=[leaf, intermediate, root], verified=True)

        assert info.chain_length == 3
        assert info.chain[0]["position"] == 0
        assert info.chain[2]["is_self_signed"] is True
        assert all("fingerprint_sha256" in link for link in info.chain)

    def test_a_malformed_chain_entry_is_skipped(self):
        leaf = make_certificate()
        info = describe(leaf, chain_der=[leaf, b"garbage"])
        assert info.chain_length == 1


class TestClassify:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (365, SslStatus.VALID.value),
            (31, SslStatus.VALID.value),
            (30, SslStatus.EXPIRING_SOON.value),
            (15, SslStatus.EXPIRING_SOON.value),
            (14, SslStatus.EXPIRING_SOON.value),
            (8, SslStatus.EXPIRING_SOON.value),
            (7, SslStatus.CRITICAL.value),
            (1, SslStatus.CRITICAL.value),
            (0, SslStatus.CRITICAL.value),
            (-1, SslStatus.EXPIRED.value),
            (None, SslStatus.UNABLE_TO_CHECK.value),
        ],
    )
    def test_default_thresholds(self, days, expected):
        """> 30 healthy, 8-30 warning, <= 7 critical, negative expired."""
        assert (
            classify_certificate(days, warning_days=30, critical_days=7) == expected
        )

    def test_thresholds_are_configurable(self):
        assert (
            classify_certificate(20, warning_days=60, critical_days=25)
            == SslStatus.CRITICAL.value
        )
        assert (
            classify_certificate(45, warning_days=60, critical_days=25)
            == SslStatus.EXPIRING_SOON.value
        )

    def test_expired_beats_every_other_state(self):
        assert (
            classify_certificate(-5, warning_days=30, critical_days=7, is_valid=True)
            == SslStatus.EXPIRED.value
        )

    def test_verification_failure_marks_invalid(self):
        assert (
            classify_certificate(
                100, warning_days=30, critical_days=7, verification_failed=True
            )
            == SslStatus.INVALID.value
        )
