"""Exercise the staged installer files without installing anything.

    python scripts/verify_stage.py build/stage

Uses only what the installer ships (embedded Python, bundled PostgreSQL): creates a
temporary database, migrates it, creates users, starts Klinik Kasa and the sync worker,
then runs a backup, a snapshot and a full restore test. Everything is deleted afterwards.
Windows services are the one part not covered: registering them needs administrator rights.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def check(ok: bool, what: str) -> None:
    print(f"    {'ok  ' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        raise SystemExit(1)


def wait_http(url: str, seconds: int = 60) -> bytes:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return bytes(r.read())
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"no answer from {url}")


def main(stage: Path) -> None:
    py = stage / "python" / "python.exe"
    pg = stage / "pgsql" / "bin"
    tmp = Path(tempfile.mkdtemp(prefix="klinik-verify-"))
    data, env_file = tmp / "pgdata", tmp / "klinik.env"
    pg_port, kasa_port, sync_port = free_port(), free_port(), free_port()
    owner = "verify-owner-pw"
    processes: list[subprocess.Popen[bytes]] = []

    def staged(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(py), "-m", "klinik_local.service", "--env", str(env_file), *args],
            capture_output=True, text=True, cwd=stage / "app", check=False,
            env={k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))},
        )  # fmt: skip
        if check_rc and result.returncode != 0:
            print(result.stdout[-3000:], result.stderr[-3000:])
            raise SystemExit(f"{args[0]} failed")
        return result

    try:
        pw = tmp / "pw.txt"
        pw.write_text(owner)
        subprocess.run([str(pg / "initdb.exe"), "-D", str(data), "-U", "kasa_migrator",
                        f"--pwfile={pw}", "--encoding=UTF8", "--locale=C",
                        "--auth=scram-sha-256", "--no-instructions"],
                       check=True, capture_output=True)  # fmt: skip
        subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(data), "-l", str(tmp / "pg.log"), "-w",
                        "-o", f"-p {pg_port} -c listen_addresses=127.0.0.1", "start"],
                       check=True, capture_output=True)  # fmt: skip
        subprocess.run([str(pg / "psql.exe"), "-h", "127.0.0.1", "-p", str(pg_port), "-U",
                        "kasa_migrator", "-d", "postgres", "-q", "-c", "CREATE DATABASE kasa"],
                       check=True, capture_output=True, env={**os.environ, "PGPASSWORD": owner})  # fmt: skip
        check(True, "bundled PostgreSQL initialises and starts")

        keys = subprocess.run(
            [str(py), "-c",
             "from cryptography.fernet import Fernet\nfrom pyrage import x25519\n"
             "i = x25519.Identity.generate()\n"
             "print(Fernet.generate_key().decode()); print(Fernet.generate_key().decode())\n"
             "print(str(i)); print(str(i.to_public()))"],
            capture_output=True, text=True, check=True,
        ).stdout.split()  # fmt: skip
        fernet, auth_key, age_private, age_public = keys
        (tmp / "identity.txt").write_text(age_private + "\n")
        base = f"127.0.0.1:{pg_port}/kasa"
        env_file.write_text(
            "\n".join(
                [
                    f"LOCAL_MIGRATION_DATABASE_URL=postgresql+asyncpg://kasa_migrator:{owner}@{base}",
                    f"LOCAL_DATABASE_URL=postgresql+asyncpg://kasa_app:apppw@{base}",
                    "LOCAL_APP_DB_PASSWORD=apppw",
                    "LOCAL_SYNC_DB_PASSWORD=syncpw",
                    "LOCAL_BACKUP_DB_PASSWORD=backuppw",
                    f"LOCAL_BACKUP_DATABASE_URL=postgresql://kasa_backup:backuppw@{base}",
                    f"LOCAL_FERNET_KEYS={fernet}",
                    f"LOCAL_AUTH_FERNET_KEY={auth_key}",
                    "LOCAL_COOKIE_SECURE=false",
                    f"SYNC_LOCAL_DATABASE_URL=postgresql+asyncpg://kasa_sync:syncpw@{base}",
                    f"BACKUP_AGE_RECIPIENT={age_public}",
                    f"LOCAL_PG_BIN={pg}",
                    f"LOCAL_BACKUP_DIR={tmp / 'backups'}",
                    f"LOCAL_SNAPSHOT_DIR={tmp / 'ozet'}",
                    f"KLINIK_ALEMBIC_INI={stage / 'app' / 'alembic.ini'}",
                    f"KLINIK_MIGRATIONS_DIR={stage / 'app' / 'migrations' / 'local'}",
                    f"KLINIK_KASA_PORT={kasa_port}",
                    f"KLINIK_SYNC_PORT={sync_port}",
                ]
            ),
            encoding="utf-8",
        )

        staged("migrate")
        check(True, "migrations and role passwords (embedded Python)")
        users = staged("manage", "bootstrap-users", str(ROOT / "infra" / "users.example.toml"))
        check("created terapist.a" in users.stdout, "users created from users.toml")

        clean_env = {
            k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))
        }
        for target in ("kasa", "sync"):
            processes.append(
                subprocess.Popen(
                    [str(py), "-m", "klinik_local.service", "--env", str(env_file), target],
                    cwd=stage / "app",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=clean_env,
                )
            )
        health = wait_http(f"http://127.0.0.1:{kasa_port}/healthz")
        check(b'"db":"ok"' in health, "Klinik Kasa answers with its database")
        login = wait_http(f"http://127.0.0.1:{kasa_port}/login").decode("utf-8")
        check(
            "Klinik Kasa" in login and "Giriş yap" in login, "login page renders (templates, theme)"
        )
        css = wait_http(f"http://127.0.0.1:{kasa_port}/static/ui/css/klinik.css")
        check(b"#356650" in css, "static files are served")
        sync = wait_http(f"http://127.0.0.1:{sync_port}/healthz")
        check(b'"db":"ok"' in sync, "sync worker starts (idle until a cloud address is set)")

        backup = staged("job", "backup", "daily")
        check("backup ok" in backup.stdout, "encrypted backup written")
        snapshot = staged("job", "snapshot")
        check("no snapshot passphrase yet" in snapshot.stdout, "snapshot job runs")
        restore = staged(
            "job", "restore-test", "--identity", str(tmp / "identity.txt"), check_rc=False
        )
        print("      " + "\n      ".join(restore.stdout.strip().splitlines()))
        check(restore.returncode == 0 and "RESULT: OK" in restore.stdout,
              "restore test into a temporary server")  # fmt: skip
        newest = next((tmp / "backups" / "daily").glob("*.dump.age"))
        refused = staged("job", "restore-live", "--identity", str(tmp / "identity.txt"),
                         "--file", str(newest), check_rc=False)  # fmt: skip
        check(refused.returncode != 0, "restore-live refuses without --yes")
        staged("job", "restore-live", "--identity", str(tmp / "identity.txt"), "--file",
               str(newest), "--yes")  # fmt: skip
        staged("migrate")
        again = staged("manage", "bootstrap-users", str(ROOT / "infra" / "users.example.toml"))
        check("already exist" in again.stdout, "restore-live brings the users back")
        print("==> staged files verified")
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(data), "-m", "fast", "-w", "stop"],
                       capture_output=True, check=False)  # fmt: skip
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
