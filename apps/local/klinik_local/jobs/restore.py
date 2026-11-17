"""Monthly restore test without Docker.

Decrypts a backup with the offline private key, restores it into a temporary PostgreSQL
cluster on a free local port (deleted afterwards), and checks it with verify_restore().
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
from pathlib import Path

import pyrage

from klinik_local.jobs.backup import record
from klinik_local.jobs.config import JobSettings
from klinik_local.restore_check import RestoreReport, verify_restore
from klinik_shared.timeutil import now_utc

ROLES_SQL = Path(__file__).parent / "roles.sql"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def newest_backup(settings: JobSettings) -> Path:
    files = sorted(
        (settings.backup_dir / "daily").glob("*.dump.age"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"no backup in {settings.backup_dir / 'daily'}")
    return files[0]


def _run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, check=False, timeout=1800)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"{Path(command[0]).name} failed: {detail[-800:]}")


async def restore_test(
    settings: JobSettings, identity_file: Path, backup_file: Path | None = None
) -> RestoreReport:
    backup = backup_file or newest_backup(settings)
    identity = pyrage.x25519.Identity.from_str(
        next(
            line.strip()
            for line in identity_file.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("AGE-SECRET-KEY-")
        )
    )
    plain = pyrage.decrypt(backup.read_bytes(), [identity])

    with tempfile.TemporaryDirectory(prefix="klinik-restore-") as tmp:
        work = Path(tmp)
        data = work / "data"
        dump = work / "backup.dump"
        dump.write_bytes(plain)
        del plain
        port = str(free_port())
        _run(settings.tool("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust",
             "--encoding=UTF8", "--no-instructions")  # fmt: skip
        _run(settings.tool("pg_ctl"), "-D", str(data), "-l", str(work / "pg.log"), "-w",
             "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start")  # fmt: skip
        try:
            base = ["-h", "127.0.0.1", "-p", port, "-U", "postgres"]
            _run(settings.tool("psql"), *base, "-d", "postgres", "-q", "-c", "CREATE DATABASE kasa")
            _run(settings.tool("psql"), *base, "-d", "kasa", "-q", "-v", "ON_ERROR_STOP=1",
                 "-f", str(ROLES_SQL))  # fmt: skip
            _run(settings.tool("pg_restore"), *base, "-d", "kasa", "--exit-on-error",
                 "--no-owner", str(dump))  # fmt: skip
            report = await verify_restore(
                f"postgresql://postgres@127.0.0.1:{port}/kasa", settings.fernet_keys
            )
        finally:
            subprocess.run(
                [settings.tool("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                capture_output=True,
                check=False,
            )
    outcome = "ok" if report.ok else "FAILED"
    await record(
        settings.backup_database_url,
        last_restore_test=f"{now_utc().isoformat()} {outcome} {backup.name}",
    )
    return report


async def restore_live(settings: JobSettings, identity_file: Path, backup_file: Path) -> None:
    """Replace the live database with a backup (rebuilding after losing the laptop).

    Destroys the current contents of database "kasa". Stop KlinikKasa, KlinikSync and
    KlinikJobs first. Connects as the owner role (LOCAL_MIGRATION_DATABASE_URL).
    """
    import os

    from klinik_local.jobs.backup import split_dsn

    owner_url = os.environ.get("LOCAL_MIGRATION_DATABASE_URL")
    if not owner_url:
        raise RuntimeError("LOCAL_MIGRATION_DATABASE_URL is not set")
    identity = pyrage.x25519.Identity.from_str(
        next(
            line.strip()
            for line in identity_file.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("AGE-SECRET-KEY-")
        )
    )
    uri, env = split_dsn(owner_url)
    admin_uri = uri.rsplit("/", 1)[0] + "/postgres"
    with tempfile.TemporaryDirectory(prefix="klinik-restore-live-") as tmp:
        dump = Path(tmp) / "backup.dump"
        dump.write_bytes(pyrage.decrypt(backup_file.read_bytes(), [identity]))

        def psql(sql: str) -> None:
            result = subprocess.run(
                [settings.tool("psql"), f"--dbname={admin_uri}", "-q", "-v", "ON_ERROR_STOP=1",
                 "-c", sql],
                capture_output=True, env=env, check=False,
            )  # fmt: skip
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode(errors="replace")[-800:])

        psql("DROP DATABASE IF EXISTS kasa WITH (FORCE)")
        psql("CREATE DATABASE kasa")
        result = subprocess.run(
            [settings.tool("psql"), f"--dbname={uri}", "-q", "-v", "ON_ERROR_STOP=1",
             "-f", str(ROLES_SQL)],
            capture_output=True, env=env, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace")[-800:])
        result = subprocess.run(
            [settings.tool("pg_restore"), f"--dbname={uri}", "--exit-on-error", "--no-owner",
             str(dump)],
            capture_output=True, env=env, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace")[-800:])
