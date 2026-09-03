"""SSL/TLS certificate inspection.

Two entry points:

* :func:`describe_certificate` turns the DER bytes captured from a live
  connection into a structured record.
* :func:`probe_tls` opens a dedicated TLS connection when no HTTP request is
  being made (``tls``/``tcp`` check types), or when the HTTP request failed
  before a handshake could be observed.

Verification status is determined honestly: the first handshake is attempted
with full verification, and only if that fails do we reconnect with
verification disabled so the certificate can still be described. That is how an
expired or self-signed certificate ends up with complete details *and* an
accurate ``chain_verified=False``.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from app.core.enums import SslStatus
from app.core.logging import get_logger

logger = get_logger(__name__)

_OID_LABELS = {
    NameOID.COMMON_NAME: "CN",
    NameOID.ORGANIZATION_NAME: "O",
    NameOID.ORGANIZATIONAL_UNIT_NAME: "OU",
    NameOID.COUNTRY_NAME: "C",
    NameOID.STATE_OR_PROVINCE_NAME: "ST",
    NameOID.LOCALITY_NAME: "L",
}


@dataclass
class CertificateInfo:
    """Everything the UI needs about one certificate observation."""

    fingerprint_sha256: str | None = None
    serial_number: str | None = None
    subject: str | None = None
    common_name: str | None = None
    issuer: str | None = None
    issuer_common_name: str | None = None
    issuer_organization: str | None = None
    san: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    days_remaining: int | None = None
    signature_algorithm: str | None = None
    key_algorithm: str | None = None
    key_size: int | None = None
    version: str | None = None
    tls_version: str | None = None
    tls_cipher: str | None = None
    is_self_signed: bool = False
    is_wildcard: bool = False
    hostname_matches: bool | None = None
    chain_verified: bool | None = None
    verification_status: str | None = None
    verification_error: str | None = None
    chain: list[dict[str, Any]] = field(default_factory=list)
    chain_length: int | None = None
    status: str = SslStatus.UNABLE_TO_CHECK.value
    handshake_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint_sha256": self.fingerprint_sha256,
            "serial_number": self.serial_number,
            "subject": self.subject,
            "common_name": self.common_name,
            "issuer": self.issuer,
            "issuer_common_name": self.issuer_common_name,
            "issuer_organization": self.issuer_organization,
            "san": self.san,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "days_remaining": self.days_remaining,
            "signature_algorithm": self.signature_algorithm,
            "key_algorithm": self.key_algorithm,
            "key_size": self.key_size,
            "version": self.version,
            "tls_version": self.tls_version,
            "tls_cipher": self.tls_cipher,
            "is_self_signed": self.is_self_signed,
            "is_wildcard": self.is_wildcard,
            "hostname_matches": self.hostname_matches,
            "chain_verified": self.chain_verified,
            "verification_status": self.verification_status,
            "verification_error": self.verification_error,
            "chain": self.chain,
            "chain_length": self.chain_length,
            "status": self.status,
        }


# --------------------------------------------------------------- formatting
def _format_name(name: x509.Name) -> str:
    parts: list[str] = []
    for attribute in name:
        label = _OID_LABELS.get(attribute.oid, attribute.oid.dotted_string)
        value = attribute.value
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        parts.append(f"{label}={value}")
    return ", ".join(parts)


def _first_value(name: x509.Name, oid: x509.ObjectIdentifier) -> str | None:
    values = name.get_attributes_for_oid(oid)
    if not values:
        return None
    value = values[0].value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _not_valid_before(cert: x509.Certificate) -> datetime | None:
    # cryptography >= 42 exposes the tz-aware variants and deprecates the
    # naive ones.
    getter = getattr(cert, "not_valid_before_utc", None)
    return _aware(getter if getter is not None else cert.not_valid_before)


def _not_valid_after(cert: x509.Certificate) -> datetime | None:
    getter = getattr(cert, "not_valid_after_utc", None)
    return _aware(getter if getter is not None else cert.not_valid_after)


def _key_details(cert: x509.Certificate) -> tuple[str | None, int | None]:
    try:
        key = cert.public_key()
    except Exception:  # pragma: no cover - malformed certificate
        return None, None
    if isinstance(key, rsa.RSAPublicKey):
        return "RSA", key.key_size
    if isinstance(key, ec.EllipticCurvePublicKey):
        return f"EC ({key.curve.name})", key.key_size
    if isinstance(key, dsa.DSAPublicKey):
        return "DSA", key.key_size
    if isinstance(key, ed25519.Ed25519PublicKey):
        return "Ed25519", 256
    if isinstance(key, ed448.Ed448PublicKey):
        return "Ed448", 456
    return type(key).__name__, getattr(key, "key_size", None)


def _san_names(cert: x509.Certificate) -> list[str]:
    names: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    except x509.ExtensionNotFound:
        return names
    san = ext.value
    try:
        names.extend(san.get_values_for_type(x509.DNSName))
    except Exception:  # pragma: no cover
        pass
    try:
        names.extend(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
    except Exception:  # pragma: no cover
        pass
    return names


def hostname_matches(hostname: str, common_name: str | None, san: list[str]) -> bool:
    """Wildcard-aware hostname match against CN and SAN entries."""
    candidates = [n for n in ([common_name] if common_name else []) + list(san) if n]
    host = hostname.lower().rstrip(".")
    for candidate in candidates:
        pattern = candidate.lower().rstrip(".")
        if pattern == host:
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix) and host.count(".") == pattern.count("."):
                return True
    return False


def classify_certificate(
    days_remaining: int | None,
    *,
    warning_days: int,
    critical_days: int,
    is_valid: bool = True,
    verification_failed: bool = False,
) -> str:
    """Map remaining days plus validity onto a display state."""
    if days_remaining is None:
        return SslStatus.UNABLE_TO_CHECK.value
    if days_remaining < 0:
        return SslStatus.EXPIRED.value
    if not is_valid or verification_failed:
        return SslStatus.INVALID.value
    if days_remaining <= critical_days:
        return SslStatus.CRITICAL.value
    if days_remaining <= warning_days:
        return SslStatus.EXPIRING_SOON.value
    return SslStatus.VALID.value


# ----------------------------------------------------------------- parsing
def describe_certificate(
    der_bytes: bytes,
    *,
    hostname: str,
    warning_days: int,
    critical_days: int,
    chain_der: list[bytes] | None = None,
    tls_version: str | None = None,
    tls_cipher: str | None = None,
    verified: bool | None = None,
    verification_error: str | None = None,
    now: datetime | None = None,
) -> CertificateInfo:
    """Build a :class:`CertificateInfo` from the leaf DER and optional chain."""
    info = CertificateInfo(tls_version=tls_version, tls_cipher=tls_cipher)
    reference = now or datetime.now(timezone.utc)

    try:
        cert = x509.load_der_x509_certificate(der_bytes)
    except Exception as exc:
        info.error = f"could not parse certificate: {exc}"
        info.status = SslStatus.INVALID.value
        info.chain_verified = verified
        info.verification_status = "unparseable"
        info.verification_error = verification_error or str(exc)
        return info

    info.fingerprint_sha256 = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    try:
        info.serial_number = format(cert.serial_number, "x").upper()
    except Exception:  # pragma: no cover
        info.serial_number = None

    info.subject = _format_name(cert.subject)
    info.issuer = _format_name(cert.issuer)
    info.common_name = _first_value(cert.subject, NameOID.COMMON_NAME)
    info.issuer_common_name = _first_value(cert.issuer, NameOID.COMMON_NAME)
    info.issuer_organization = _first_value(cert.issuer, NameOID.ORGANIZATION_NAME)
    info.san = _san_names(cert)
    info.version = getattr(cert.version, "name", None)

    try:
        info.signature_algorithm = cert.signature_algorithm_oid._name  # noqa: SLF001
    except Exception:  # pragma: no cover
        info.signature_algorithm = None
    if not info.signature_algorithm:
        algo = getattr(cert, "signature_hash_algorithm", None)
        info.signature_algorithm = getattr(algo, "name", None)

    info.key_algorithm, info.key_size = _key_details(cert)

    info.valid_from = _not_valid_before(cert)
    info.valid_to = _not_valid_after(cert)
    if info.valid_to is not None:
        delta = info.valid_to - reference
        # Round toward zero on the day boundary: 23h left reads as 0 days, not
        # 1, so "< 7 days" alerts do not fire a day late.
        info.days_remaining = int(delta.total_seconds() // 86400)

    info.is_self_signed = cert.subject == cert.issuer
    info.is_wildcard = any(name.startswith("*.") for name in info.san) or bool(
        info.common_name and info.common_name.startswith("*.")
    )
    info.hostname_matches = hostname_matches(hostname, info.common_name, info.san)

    # ------------------------------------------------------------- chain
    chain_entries: list[dict[str, Any]] = []
    for index, raw in enumerate(chain_der or []):
        try:
            link = x509.load_der_x509_certificate(raw)
        except Exception:  # pragma: no cover
            continue
        chain_entries.append(
            {
                "position": index,
                "subject": _format_name(link.subject),
                "common_name": _first_value(link.subject, NameOID.COMMON_NAME),
                "issuer": _format_name(link.issuer),
                "issuer_common_name": _first_value(link.issuer, NameOID.COMMON_NAME),
                "valid_from": _iso(_not_valid_before(link)),
                "valid_to": _iso(_not_valid_after(link)),
                "is_self_signed": link.subject == link.issuer,
                "fingerprint_sha256": link.fingerprint(hashes.SHA256())
                .hex(":")
                .upper(),
            }
        )
    info.chain = chain_entries
    info.chain_length = len(chain_entries) or None

    # -------------------------------------------------------- validity
    info.chain_verified = verified
    info.verification_error = verification_error
    expired = info.days_remaining is not None and info.days_remaining < 0
    not_yet_valid = info.valid_from is not None and info.valid_from > reference

    if expired:
        info.verification_status = "expired"
    elif not_yet_valid:
        info.verification_status = "not_yet_valid"
    elif verified is False:
        info.verification_status = "verification_failed"
    elif info.hostname_matches is False:
        info.verification_status = "hostname_mismatch"
    elif verified is True:
        info.verification_status = "verified"
    else:
        info.verification_status = "unverified"

    structurally_valid = (
        not expired
        and not not_yet_valid
        and info.hostname_matches is not False
        and verified is not False
    )
    info.status = classify_certificate(
        info.days_remaining,
        warning_days=warning_days,
        critical_days=critical_days,
        is_valid=structurally_valid,
        verification_failed=verified is False,
    )
    return info


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def extract_from_ssl_object(
    ssl_object: ssl.SSLObject | ssl.SSLSocket,
) -> tuple[bytes | None, list[bytes], str | None, str | None]:
    """Pull leaf DER, chain DER, protocol and cipher off a live TLS object."""
    leaf: bytes | None = None
    chain: list[bytes] = []
    try:
        leaf = ssl_object.getpeercert(binary_form=True)
    except Exception:  # pragma: no cover
        leaf = None

    # Available from CPython 3.13 onwards; a no-op on older runtimes.
    for getter in ("get_verified_chain", "get_unverified_chain"):
        method = getattr(ssl_object, getter, None)
        if method is None:
            continue
        try:
            certs = method()
        except Exception:
            continue
        if not certs:
            continue
        for cert in certs:
            der = cert if isinstance(cert, bytes) else _to_der(cert)
            if der:
                chain.append(der)
        if chain:
            break

    version = None
    cipher_name = None
    try:
        version = ssl_object.version()
    except Exception:  # pragma: no cover
        pass
    try:
        cipher = ssl_object.cipher()
        if cipher:
            cipher_name = cipher[0]
    except Exception:  # pragma: no cover
        pass
    return leaf, chain, version, cipher_name


def _to_der(cert: Any) -> bytes | None:
    """Best-effort conversion of an ``ssl.Certificate`` object to DER."""
    for attr in ("public_bytes",):
        method = getattr(cert, attr, None)
        if method is None:
            continue
        try:
            return method(serialization.Encoding.DER)
        except TypeError:
            try:
                return method()
            except Exception:
                return None
        except Exception:
            return None
    return None


# -------------------------------------------------------------- live probe
def build_ssl_context(*, verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


async def probe_tls(
    hostname: str,
    port: int,
    *,
    timeout: float,
    warning_days: int,
    critical_days: int,
    server_hostname: str | None = None,
    verify: bool = True,
    resolved_ip: str | None = None,
) -> CertificateInfo:
    """Open a TLS connection and describe the presented certificate.

    Attempts a verifying handshake first; on verification failure it retries
    without verification so the certificate can still be reported, with the
    original verification error preserved.
    """
    sni = server_hostname or hostname
    connect_host = resolved_ip or hostname
    verification_error: str | None = None
    verified: bool | None = None

    attempts: list[bool] = [True, False] if verify else [False]
    for attempt_verify in attempts:
        context = build_ssl_context(verify=attempt_verify)
        started = perf_counter()
        writer = None
        try:
            reader_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=connect_host,
                    port=port,
                    ssl=context,
                    server_hostname=sni,
                ),
                timeout=timeout,
            )
            _, writer = reader_writer
            handshake_ms = (perf_counter() - started) * 1000.0

            ssl_object = writer.get_extra_info("ssl_object")
            if ssl_object is None:
                info = CertificateInfo(
                    error="TLS handshake produced no SSL object",
                    status=SslStatus.UNABLE_TO_CHECK.value,
                )
                info.handshake_ms = handshake_ms
                return info

            leaf, chain, version, cipher = extract_from_ssl_object(ssl_object)
            if not leaf:
                info = CertificateInfo(
                    error="peer presented no certificate",
                    status=SslStatus.INVALID.value,
                    tls_version=version,
                    tls_cipher=cipher,
                )
                info.handshake_ms = handshake_ms
                return info

            info = describe_certificate(
                leaf,
                hostname=sni,
                warning_days=warning_days,
                critical_days=critical_days,
                chain_der=chain,
                tls_version=version,
                tls_cipher=cipher,
                verified=attempt_verify if verification_error is None else False,
                verification_error=verification_error,
            )
            info.handshake_ms = handshake_ms
            return info

        except ssl.SSLCertVerificationError as exc:
            verification_error = str(exc.verify_message or exc)
            verified = False
            if attempt_verify:
                continue  # retry without verification to read the cert
            return CertificateInfo(
                error=verification_error,
                status=SslStatus.INVALID.value,
                chain_verified=False,
                verification_status="verification_failed",
                verification_error=verification_error,
            )
        except ssl.SSLError as exc:
            message = str(exc)
            if attempt_verify:
                verification_error = message
                verified = False
                continue
            return CertificateInfo(
                error=message,
                status=SslStatus.INVALID.value,
                chain_verified=verified,
                verification_status="tls_error",
                verification_error=message,
            )
        except asyncio.TimeoutError:
            return CertificateInfo(
                error=f"TLS handshake timed out after {timeout:g}s",
                status=SslStatus.UNABLE_TO_CHECK.value,
                verification_status="timeout",
            )
        except (OSError, socket.gaierror) as exc:
            return CertificateInfo(
                error=f"could not connect for TLS inspection: {exc}",
                status=SslStatus.UNABLE_TO_CHECK.value,
                verification_status="connection_failed",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "tls_probe_unexpected_error", hostname=hostname, error=str(exc)
            )
            return CertificateInfo(
                error=str(exc), status=SslStatus.UNABLE_TO_CHECK.value
            )
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # pragma: no cover
                    pass

    return CertificateInfo(
        error=verification_error or "TLS inspection failed",
        status=SslStatus.UNABLE_TO_CHECK.value,
        chain_verified=verified,
        verification_error=verification_error,
    )
