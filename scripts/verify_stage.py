"""Exercise the staged installer files without installing anything.

    python scripts/verify_stage.py build/stage

Uses only what the installer ships (embedded Python, bundled PostgreSQL): creates a
temporary database, migrates it, creates the admin, starts the app over HTTPS with a
freshly made certificate, renders a PDF report, then runs a backup, a snapshot and a full
restore test. Everything is deleted afterwards.
Windows services are the one part not covered: registering them needs administrator rights.
"""

from __future__ import annotations

import os
import socket
import ssl
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


def wait_http(url: str, seconds: int = 60, context: ssl.SSLContext | None = None) -> bytes:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3, context=context) as r:
                return bytes(r.read())
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"no answer from {url}")


def main(stage: Path) -> None:
    py = stage / "python" / "python.exe"
    pg = stage / "pgsql" / "bin"
    tmp = Path(tempfile.mkdtemp(prefix="klinik-verify-"))
    data, env_file = tmp / "pgdata", tmp / "klinik.env"
    pg_port, app_port = free_port(), free_port()
    owner = "verify-owner-pw"
    processes: list[subprocess.Popen[bytes]] = []

    def staged(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(py), "-m", "klinik_local.service", "--env", str(env_file), *args],
            capture_output=True, text=True, cwd=stage / "app", check=False, timeout=900,
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
                       check=True, capture_output=True, timeout=300)  # fmt: skip
        # Never a pipe for the server's output (Windows: it would never close; see restore.py).
        subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(data), "-l", str(tmp / "pg.log"), "-w",
                        "-o", f"-p {pg_port} -c listen_addresses=127.0.0.1", "start"],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=180)  # fmt: skip
        subprocess.run([str(pg / "psql.exe"), "-h", "127.0.0.1", "-p", str(pg_port), "-U",
                        "kasa_migrator", "-d", "postgres", "-q", "-c", "CREATE DATABASE kasa"],
                       check=True, capture_output=True, timeout=120,
                       env={**os.environ, "PGPASSWORD": owner})  # fmt: skip
        check(True, "bundled PostgreSQL initialises and starts")

        keys = subprocess.run(
            [str(py), "-c",
             "from cryptography.fernet import Fernet\nfrom pyrage import x25519\n"
             "i = x25519.Identity.generate()\n"
             "print(Fernet.generate_key().decode()); print(Fernet.generate_key().decode())\n"
             "print(str(i)); print(str(i.to_public()))"],
            capture_output=True, text=True, check=True, timeout=120,
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
                    "LOCAL_COOKIE_SECURE=true",
                    "LOCAL_MODE=office",
                    f"LOCAL_SYNC_DATABASE_URL=postgresql+asyncpg://kasa_sync:syncpw@{base}",
                    f"LOCAL_TLS_CERT={tmp / 'tls' / 'klinik.crt'}",
                    f"KLINIK_TLS_CERT={tmp / 'tls' / 'klinik.crt'}",
                    f"KLINIK_TLS_KEY={tmp / 'tls' / 'klinik.key'}",
                    "KLINIK_BIND=127.0.0.1",
                    f"BACKUP_AGE_RECIPIENT={age_public}",
                    f"LOCAL_PG_BIN={pg}",
                    f"LOCAL_BACKUP_DIR={tmp / 'backups'}",
                    f"LOCAL_SNAPSHOT_DIR={tmp / 'ozet'}",
                    f"KLINIK_ALEMBIC_INI={stage / 'app' / 'alembic.ini'}",
                    f"KLINIK_MIGRATIONS_DIR={stage / 'app' / 'migrations' / 'local'}",
                    f"KLINIK_PORT={app_port}",
                ]
            ),
            encoding="utf-8",
        )

        staged("migrate")
        check(True, "migrations and role passwords (embedded Python)")
        admin = staged("manage", "create-user", "--username", "yonetici", "--name", "Klinik Sahibi")
        check("created yonetici" in admin.stdout, "admin user created")
        cert = staged("manage", "generate-cert", "--out", str(tmp / "tls"), "--name", "localhost",
                      "--name", "127.0.0.1")  # fmt: skip
        check("SHA-256" in cert.stdout, "HTTPS certificate created")
        trust = ssl.create_default_context(cafile=str(tmp / "tls" / "klinik.crt"))

        clean_env = {
            k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))
        }
        processes.append(
            subprocess.Popen(
                [str(py), "-m", "klinik_local.service", "--env", str(env_file), "app"],
                cwd=stage / "app",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=clean_env,
            )
        )
        url = f"https://localhost:{app_port}"
        health = wait_http(f"{url}/healthz", context=trust)
        check(b'"db":"ok"' in health, "the app answers over HTTPS with its database")
        login = wait_http(f"{url}/login", context=trust).decode("utf-8")
        check("Giriş yap" in login, "login page renders (templates, theme)")
        css = wait_http(f"{url}/static/ui/css/klinik.css", context=trust)
        check(b"#356650" in css, "static files are served")
        api = subprocess.run(
            [str(py), "-c",
             "import ssl, sys, urllib.request, urllib.error\n"
             "ctx = ssl.create_default_context(cafile=sys.argv[2])\n"
             "req = urllib.request.Request(sys.argv[1], data=b'{}', method='POST')\n"
             "try:\n    urllib.request.urlopen(req, context=ctx)\n"
             "except urllib.error.HTTPError as e:\n    print(e.code)",
             f"{url}/replika/api/pull", str(tmp / "tls" / "klinik.crt")],
            capture_output=True, text=True, check=False, env=clean_env, timeout=60,
        )  # fmt: skip
        check(api.stdout.strip() == "401", "laptop sync API is there and refuses without a token")
        pdf = subprocess.run(
            [str(py), "-c",
             "from datetime import datetime, timezone\n"
             "from klinik_local.raporlar import pdf\n"
             "meta = pdf.Meta('Deneme', 'Türkçe: ğüşıöç İ', 'Test', datetime.now(timezone.utc))\n"
             "data = pdf.build(meta, pdf.heading('Şeker İğne Ölçü', ['Çalışma']))\n"
             "print(data[:5].decode(), len(data))"],
            capture_output=True, text=True, check=False, cwd=stage / "app", env=clean_env,
            timeout=120,
        )  # fmt: skip
        check(pdf.stdout.startswith("%PDF-"), "PDF reports render with the bundled font")

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
        again = staged("manage", "create-user", "--username", "yonetici", "--name", "X")
        check("exists yonetici" in again.stdout, "restore-live brings the users back")
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
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=90)  # fmt: skip
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
