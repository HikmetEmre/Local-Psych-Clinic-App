"""The office computer's own HTTPS certificate (no internet, no certificate authority).

The installer creates one self-signed certificate for the office computer and trusts it
on that computer, so browsers there open https://localhost without warnings. The admin
laptop does not need to trust it system-wide: it receives the certificate in the pairing
file and accepts exactly this certificate when it talks to the office (pinning).
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_YEARS = 10
USAGE = x509.KeyUsage(
    digital_signature=True, key_cert_sign=True, crl_sign=False,
    content_commitment=False, key_encipherment=False, data_encipherment=False,
    key_agreement=False, encipher_only=False, decipher_only=False,
)  # fmt: skip


def default_names() -> list[str]:
    host = socket.gethostname()
    return sorted({"localhost", host, host.lower(), f"{host}.local".lower(), "127.0.0.1"})


def generate(names: list[str]) -> tuple[bytes, bytes]:
    """Returns (certificate PEM, private key PEM) for the given DNS names / IP addresses."""
    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Nil Psikoloji"),
            x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "localhost"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * VALID_YEARS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(USAGE, critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def write(directory: Path, names: list[str]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_pem, key_pem = generate(names)
    cert_path, key_path = directory / "klinik.crt", directory / "klinik.key"
    key_path.write_bytes(key_pem)
    cert_path.write_bytes(cert_pem)
    return cert_path, key_path


def fingerprint(cert_pem: bytes) -> str:
    """SHA-256 of the certificate, as shown to people (AB:CD:...)."""
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
