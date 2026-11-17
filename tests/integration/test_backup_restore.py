"""The real backup script, end to end: dump -> encrypt -> decrypt -> restore -> verify.

Definition of done (brief §16): the latest backup restores successfully. This runs the
same image and script as the office laptop, against a real database with encrypted data.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from support.local import (
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_therapists,
    run,
    scalar,
    with_engine,
)

from klinik_local import manage
from klinik_local.crypto.fernet import generate_key
from klinik_local.restore_check import verify_restore

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "klinik-backup-test"
ROLES_SQL = ROOT / "apps" / "local" / "klinik_local" / "jobs" / "roles.sql"
NOTE = "Restore test notu: çocuk oyun sırasında rahatladı."


def docker(*args: str, check: bool = True, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, input=input_bytes, check=False)
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} failed ({result.returncode}): "
            f"{result.stdout.decode(errors='replace')[-1500:]} "
            f"{result.stderr.decode(errors='replace')[-1500:]}"
        )
    return result.stdout.decode(errors="replace")


@pytest.fixture(scope="session")
def backup_image() -> str:
    docker("build", "-q", "-t", IMAGE, "-f", str(ROOT / "infra/backup/Dockerfile"), str(ROOT))
    return IMAGE


@pytest.fixture
def age_keys(backup_image: str, tmp_path: Path) -> tuple[Path, str]:
    output = docker("run", "--rm", "--entrypoint", "age-keygen", backup_image)
    identity = tmp_path / "identity.txt"
    identity.write_text(output, encoding="utf-8")
    recipient = re.search(r"public key: (age1[0-9a-z]+)", output)
    assert recipient
    return identity, recipient.group(1)


@pytest.fixture
def populated(local_db: LocalDb) -> Iterator[LocalDb]:
    """An office database with users, a client and a session with an encrypted note."""
    temporary = add_therapists(local_db)
    with_engine(
        local_db.owner_url,
        lambda e: manage.set_role_passwords(e, {"kasa_backup": "backup_pw"}),
    )
    with Kasa(local_db) as kasa:
        kasa.onboard("terapist.a", temporary["a"])
        created = kasa.post("/danisanlar", {"full_name": "Elif Kaya", "birth_date": "2016-05-01"})
        client = created.headers["location"].split("/")[2].split("?")[0]
        session = kasa.api(
            "POST",
            "/api/therapy/sessions",
            json={
                "client_id": client,
                "session_date": "2026-11-10T14:00:00+03:00",
                "format_code": "individual",
                "methods": [{"code": "play_therapy"}],
                "notes": NOTE,
            },
        )
        assert session.status_code == 201
    yield local_db


def container_url(url: str, user: str, password: str) -> tuple[str, str, str]:
    """(host, port, database) of the test server as seen from another container."""
    parsed = urlparse(url.replace("+asyncpg", ""))
    return "host.docker.internal", str(parsed.port), parsed.path.lstrip("/")


def run_backup(db: LocalDb, image: str, recipient: str, backups: Path, kind: str = "daily") -> str:
    host, port, name = container_url(db.owner_url, "kasa_backup", "backup_pw")
    return docker(
        "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
        "-e", f"PGHOST={host}", "-e", f"PGPORT={port}", "-e", f"PGDATABASE={name}",
        "-e", "PGUSER=kasa_backup", "-e", "PGPASSWORD=backup_pw",
        "-e", f"AGE_RECIPIENT={recipient}",
        "-v", f"{backups}:/backups",
        "--user", "0",
        image, "klinik-backup", kind,
    )  # fmt: skip


def restore_into_new_server(image: str, identity: Path, encrypted: Path) -> Iterator[str]:
    """Decrypt, then restore into a brand-new PostgreSQL server (as after losing the laptop)."""
    docker(
        "run", "--rm", "-v", f"{encrypted.parent}:/work", "-v", f"{identity}:/key.txt:ro",
        "--user", "0", "--entrypoint", "age", image,
        "--decrypt", "--identity", "/key.txt", "--output", "/work/restored.dump",
        f"/work/{encrypted.name}",
    )  # fmt: skip
    name = f"klinik-restore-{uuid.uuid4().hex[:8]}"
    docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_PASSWORD=restore", "-e", "POSTGRES_DB=kasa",
        "-v", f"{encrypted.parent}:/work", "-v", f"{ROLES_SQL}:/roles.sql:ro",
        "postgres:16-alpine",
    )  # fmt: skip
    try:
        for _ in range(60):
            if subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "kasa"],
                capture_output=True, check=False,
            ).returncode == 0:  # fmt: skip
                break
            time.sleep(0.5)
        time.sleep(1)  # the image restarts once after initialising
        for _ in range(60):
            if subprocess.run(
                ["docker", "exec", name, "psql", "-U", "postgres", "-d", "kasa", "-c", "select 1"],
                capture_output=True, check=False,
            ).returncode == 0:  # fmt: skip
                break
            time.sleep(0.5)
        docker("exec", name, "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
               "-d", "kasa", "-f", "/roles.sql")  # fmt: skip
        docker("exec", name, "pg_restore", "--exit-on-error", "--no-owner", "-U", "postgres",
               "-d", "kasa", "/work/restored.dump")  # fmt: skip
        port = docker("port", name, "5432").strip().splitlines()[0].rsplit(":", 1)[1]
        yield f"postgresql://postgres:restore@127.0.0.1:{port}/kasa"
    finally:
        docker("rm", "-f", name, check=False)


def test_backup_restores_and_its_notes_are_readable(
    populated: LocalDb,
    backup_image: str,
    age_keys: tuple[Path, str],
    tmp_path: Path,
) -> None:
    identity, recipient = age_keys
    backups = tmp_path / "backups"
    backups.mkdir()
    output = run_backup(populated, backup_image, recipient, backups)
    assert "backup ok" in output
    files = list((backups / "daily").glob("*.dump.age"))
    assert len(files) == 1

    # The file is useless without the private key: no plaintext inside.
    raw = files[0].read_bytes()
    assert raw.startswith(b"age-encryption.org/v1")
    assert b"Elif" not in raw and b"terapist.a" not in raw and b"CREATE TABLE" not in raw

    # The job reported itself to Klinik Kasa.
    assert scalar(populated, "SELECT value FROM backup_status WHERE key = 'last_daily_ok'")

    restored = restore_into_new_server(backup_image, identity, files[0])
    restored_dsn = next(restored)
    report = run(verify_restore(restored_dsn, FERNET_KEY))
    assert report.ok, report.lines()
    assert report.counts["clients"] == 1 and report.counts["therapy_sessions"] == 1
    assert report.names_checked == 1 and report.notes_checked == 1
    assert report.schema_version == report.expected_version

    # With other keys the same backup is reported as unreadable.
    wrong = run(verify_restore(restored_dsn, generate_key()))
    assert not wrong.ok
    assert any("cannot be decrypted" in p for p in wrong.problems)
    # Row-level security came back too: the app role sees no session without a user.
    rls = run(_rls_enabled(restored_dsn))
    assert rls == {"clients": True, "therapy_sessions": True}
    restored.close()


async def _rls_enabled(dsn: str) -> dict[str, bool]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT relname, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('clients', 'therapy_sessions')"
        )
        return {r["relname"]: r["relforcerowsecurity"] for r in rows}
    finally:
        await conn.close()


def test_a_different_private_key_cannot_open_the_backup(
    populated: LocalDb, backup_image: str, age_keys: tuple[Path, str], tmp_path: Path
) -> None:
    _, recipient = age_keys
    backups = tmp_path / "backups"
    backups.mkdir()
    run_backup(populated, backup_image, recipient, backups)
    stranger = tmp_path / "other.txt"
    stranger.write_text(docker("run", "--rm", "--entrypoint", "age-keygen", backup_image))
    encrypted = next((backups / "daily").glob("*.dump.age"))
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{backups / 'daily'}:/work",
            "-v",
            f"{stranger}:/key.txt:ro",
            "--entrypoint",
            "age",
            backup_image,
            "--decrypt",
            "--identity",
            "/key.txt",
            f"/work/{encrypted.name}",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_retention_keeps_only_the_newest_hourly_backups(
    populated: LocalDb, backup_image: str, age_keys: tuple[Path, str], tmp_path: Path
) -> None:
    _, recipient = age_keys
    hourly = tmp_path / "backups" / "hourly"
    hourly.mkdir(parents=True)
    for n in range(50):  # older files, oldest first
        old = hourly / f"kasa-2026010{n % 10}-{n:04d}.dump.age"
        old.write_bytes(b"x" * 200)
        os.utime(old, (1_700_000_000 + n, 1_700_000_000 + n))
    run_backup(populated, backup_image, recipient, tmp_path / "backups", kind="hourly")
    kept = sorted(p.name for p in hourly.glob("*.dump.age"))
    assert len(kept) == 48
    assert "kasa-20260100-0000.dump.age" not in kept  # the oldest went first
