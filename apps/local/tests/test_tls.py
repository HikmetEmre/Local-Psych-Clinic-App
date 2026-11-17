"""The office certificate and the laptop's pinning of it."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

import pytest
from cryptography import x509

from klinik_local import tls
from klinik_local.replika.laptop import pinned_context
from klinik_local.replika.pairing import PairingError, open_sealed, seal


def test_certificate_names_and_fingerprint(tmp_path: Path) -> None:
    cert_path, key_path = tls.write(tmp_path, ["OFIS-PC", "localhost", "127.0.0.1"])
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "OFIS-PC" in names.get_values_for_type(x509.DNSName)
    assert len(tls.fingerprint(cert_path.read_bytes()).split(":")) == 32
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


async def _handshake(server_dir: Path, pinned_pem: str) -> None:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(server_dir / "klinik.crt", server_dir / "klinik.key")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        # Connect by IP, a name the laptop might use: pinning does not depend on it.
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=pinned_context(pinned_pem), server_hostname="ofis-pc"
        )
        assert await reader.read(2) == b"ok"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


def test_the_laptop_accepts_exactly_the_pinned_office(tmp_path: Path) -> None:
    office, other = tmp_path / "office", tmp_path / "other"
    tls.write(office, ["OFIS-PC"])
    tls.write(other, ["OFIS-PC"])
    asyncio.run(_handshake(office, (office / "klinik.crt").read_text()))
    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(_handshake(office, (other / "klinik.crt").read_text()))


def test_pairing_file_round_trip() -> None:
    sealed = seal({"token": "abc", "fernet_keys": "k"}, "cok-gizli-parola")
    assert b"abc" not in sealed
    assert open_sealed(sealed, "cok-gizli-parola")["token"] == "abc"
    with pytest.raises(PairingError):
        open_sealed(sealed, "baska-bir-parola")
    with pytest.raises(PairingError):
        open_sealed(b'{"kind": "x"}', "cok-gizli-parola")
    with pytest.raises(PairingError, match="en az"):
        seal({}, "kisa")
