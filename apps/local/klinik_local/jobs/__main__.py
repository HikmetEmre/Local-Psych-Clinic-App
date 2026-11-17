"""Entry point of the jobs service and its one-off commands (see package docstring)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg

from klinik_local.jobs.backup import BackupError, rclone, record, run_backup
from klinik_local.jobs.config import JobSettings
from klinik_local.jobs.restore import restore_live, restore_test
from klinik_local.snapshot import write_snapshots
from klinik_shared.timeutil import now_utc, to_istanbul

log = logging.getLogger("klinik_jobs")


async def snapshot(settings: JobSettings) -> int:
    result = await write_snapshots(
        settings.backup_database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
        settings.fernet_keys,
        settings.snapshot_dir,
    )
    uploaded = None
    if settings.rclone_remote:
        uploaded = await asyncio.to_thread(
            rclone, settings, "sync", str(settings.snapshot_dir),
            f"{settings.rclone_remote}/ozet", "--include", "*.html",
        )  # fmt: skip
    status = {
        "last_snapshot_ok": now_utc().isoformat(),
        "snapshot_files": str(len(result.files)),
        "snapshot_skipped": ", ".join(result.skipped_users) or "-",
    }
    if uploaded is False:
        status["last_error"] = f"{now_utc().isoformat()} snapshot upload failed"
    await record(settings.backup_database_url, **status)
    for path in result.files:
        print(f"snapshot: {path}")
    if result.skipped_users:
        print(f"no snapshot passphrase yet: {', '.join(result.skipped_users)}")
    return 0


def daily_slot(now: datetime, hour: int) -> datetime:
    """The most recent daily run time (today at `hour`, or yesterday if not reached yet)."""
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


async def last_daily_ok(settings: JobSettings) -> datetime | None:
    try:
        conn = await asyncpg.connect(
            settings.backup_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        )
        try:
            value = await conn.fetchval(
                "SELECT value FROM backup_status WHERE key = 'last_daily_ok'"
            )
        finally:
            await conn.close()
        return datetime.fromisoformat(value) if value else None
    except Exception:
        log.exception("could not read backup status")
        return None


async def scheduler(settings: JobSettings) -> None:
    """Daily backup and snapshot at 17:00 (SNAPSHOT_HOUR), backups hourly 09-21.

    The main computer is a laptop that may be closed at 17:00: the daily backup and the
    snapshot then run as soon as it is on again (catch-up), never more than once per day.
    """
    done: dict[str, str] = {}
    log.info("jobs scheduler started")

    async def once(key: str, slot: str, job: object) -> None:
        if done.get(key) == slot:
            return
        try:
            await job  # type: ignore[misc]
            done[key] = slot
        except BackupError as exc:
            log.error("%s failed: %s", key, exc)
            done[key] = slot  # do not hammer; next slot retries
        except Exception:
            log.exception("%s crashed", key)
            done[key] = slot

    while True:
        now = to_istanbul(now_utc())
        day = now.date().isoformat()
        slot = daily_slot(now, settings.snapshot_hour)
        if done.get("daily") != slot.isoformat():
            last = await last_daily_ok(settings)
            if last is None or to_istanbul(last) < slot:
                await once("daily", slot.isoformat(), run_backup(settings, "daily"))
            else:
                done["daily"] = slot.isoformat()
        await once("snapshot", slot.isoformat(), snapshot(settings))
        if 9 <= now.hour <= 21:
            await once("hourly", f"{day}-{now.hour}", run_backup(settings, "hourly"))
        await asyncio.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(prog="klinik_local.jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="scheduler (runs as the KlinikJobs service)")
    backup = sub.add_parser("backup", help="one backup now")
    backup.add_argument("kind", choices=["daily", "hourly"])
    sub.add_parser("snapshot", help="write the daily snapshots now")
    restore = sub.add_parser("restore-test", help="monthly restore test")
    restore.add_argument("--identity", required=True, type=Path, help="offline age key file")
    restore.add_argument("--file", type=Path, default=None, help="backup file (default: newest)")
    live = sub.add_parser(
        "restore-live", help="REPLACE the live database with a backup (stop the services first)"
    )
    live.add_argument("--identity", required=True, type=Path, help="offline age key file")
    live.add_argument("--file", required=True, type=Path, help="backup file to restore")
    live.add_argument("--yes", action="store_true", help="confirm: current data is replaced")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = JobSettings()

    if args.command == "run":
        asyncio.run(scheduler(settings))
    elif args.command == "backup":
        result = asyncio.run(run_backup(settings, args.kind))
        print(f"backup ok: {result.path} ({result.size} bytes)")
    elif args.command == "snapshot":
        sys.exit(asyncio.run(snapshot(settings)))
    elif args.command == "restore-test":
        report = asyncio.run(restore_test(settings, args.identity, args.file))
        for line in report.lines():
            print(line)
        sys.exit(0 if report.ok else 1)
    elif args.command == "restore-live":
        if not args.yes:
            sys.exit("restore-live replaces the current database; repeat with --yes to confirm")
        asyncio.run(restore_live(settings, args.identity, args.file))
        print("restored. Now run: python -m klinik_local.service --env <klinik.env> migrate")


if __name__ == "__main__":
    main()
