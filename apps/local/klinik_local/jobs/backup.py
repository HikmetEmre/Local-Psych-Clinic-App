"""Encrypted backups without Docker: pg_dump -> age (public key) -> folder -> Google Drive.

Same files and retention as infra/backup/backup.sh, so either side can restore the other's.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pyrage

from klinik_local.jobs.config import JobSettings
from klinik_shared.timeutil import now_utc, to_istanbul

RETENTION = {"daily": 30, "monthly": 12, "hourly": 48}
MIN_SIZE = 100


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    uploaded: bool | None  # None: no remote configured


def split_dsn(url: str) -> tuple[str, dict[str, str]]:
    """A libpq URI without the password, and the password as PGPASSWORD.

    Keeps the password out of the process list.
    """
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    netloc = parsed.hostname or "127.0.0.1"
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl(), env


async def record(dsn: str, **values: str) -> None:
    """Report to backup_status so Klinik Kasa shows it (Ayarlar -> Yedekleme)."""
    try:
        conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))
    except Exception:
        return
    try:
        for key, value in values.items():
            await conn.execute(
                "INSERT INTO backup_status (key, value, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                key,
                value[:300],
            )
    finally:
        await conn.close()


def prune(folder: Path, keep: int) -> int:
    files = sorted(folder.glob("*.dump.age"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return max(0, len(files) - keep)


def rclone(settings: JobSettings, *args: str) -> bool:
    if not settings.rclone_remote:
        return False
    command = [settings.rclone_exe, *args, "--retries", "5", "--low-level-retries", "10"]
    if settings.rclone_config is not None:
        command += ["--config", str(settings.rclone_config)]
    result = subprocess.run(command, capture_output=True, check=False, timeout=1800)
    return result.returncode == 0


async def run_backup(settings: JobSettings, kind: str) -> BackupResult:
    if kind not in ("daily", "hourly"):
        raise ValueError(kind)
    stamp = to_istanbul(now_utc()).strftime("%Y%m%d-%H%M")
    folder = settings.backup_dir / kind
    folder.mkdir(parents=True, exist_ok=True)
    uri, env = split_dsn(settings.backup_database_url)
    target = folder / f"kasa-{stamp}.dump.age"

    command = [
        settings.tool("pg_dump"), "--format=custom", "--compress=6", "--no-owner",
        "--no-privileges", f"--dbname={uri}",
    ]  # fmt: skip
    dump = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, env=env, check=False, timeout=3600
    )
    if dump.returncode != 0:
        message = dump.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["?"]
        await record(
            settings.backup_database_url,
            last_error=f"{now_utc().isoformat()} {kind}: pg_dump: {message[0]}",
        )
        raise BackupError(f"pg_dump failed: {message[0]}")

    recipient = pyrage.x25519.Recipient.from_str(settings.age_recipient.strip())
    encrypted = pyrage.encrypt(dump.stdout, [recipient])
    del dump  # the plaintext dump never touches the disk
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(encrypted)
    os.replace(partial, target)
    size = target.stat().st_size
    if size <= MIN_SIZE:
        raise BackupError(f"backup suspiciously small ({size} bytes)")

    if kind == "daily" and to_istanbul(now_utc()).day == 1:
        monthly = settings.backup_dir / "monthly"
        monthly.mkdir(exist_ok=True)
        (monthly / f"kasa-{to_istanbul(now_utc()):%Y%m}.dump.age").write_bytes(encrypted)
    for name, keep in RETENTION.items():
        if (settings.backup_dir / name).exists():
            prune(settings.backup_dir / name, keep)

    uploaded: bool | None = None
    if settings.rclone_remote:
        uploaded = await asyncio.to_thread(
            rclone, settings, "sync", str(settings.backup_dir),
            f"{settings.rclone_remote}/yedek", "--include", "*.dump.age",
        )  # fmt: skip
    status = {
        f"last_{kind}_ok": now_utc().isoformat(),
        "last_file": f"{target.name} ({size} bytes)",
    }
    if uploaded:
        status["last_upload_ok"] = now_utc().isoformat()
    elif uploaded is False:
        status["last_error"] = f"{now_utc().isoformat()} upload to Google Drive failed"
    await record(settings.backup_database_url, **status)
    return BackupResult(target, size, uploaded)
