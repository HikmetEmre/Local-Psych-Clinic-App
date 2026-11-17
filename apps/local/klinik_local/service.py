"""Launcher for the native Windows installation (services and setup steps).

    python -m klinik_local.service --env <klinik.env> app         the web app (HTTPS)
    python -m klinik_local.service --env <klinik.env> jobs        backups, snapshot, laptop sync
    python -m klinik_local.service --env <klinik.env> migrate     migrations + role passwords
    python -m klinik_local.service --env <klinik.env> manage ...  klinik_local.manage commands
    python -m klinik_local.service --env <klinik.env> job ...     klinik_local.jobs commands

The env file lives in ProgramData, readable only by SYSTEM and Administrators. Windows
services cannot take an env file themselves, so this loads it before anything starts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key.strip()] = value


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    ini = os.environ["KLINIK_ALEMBIC_INI"]
    command.upgrade(Config(ini, ini_section="local"), "head")
    sys.argv = ["klinik_local.manage", "set-role-passwords"]
    from klinik_local import manage

    manage.main()


def main() -> None:
    parser = argparse.ArgumentParser(prog="klinik_local.service")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("target", choices=["app", "jobs", "migrate", "manage", "job"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    load_env(args.env)

    if args.target == "app":
        import uvicorn

        # Office: KLINIK_BIND=0.0.0.0 so the admin laptop can reach it on the office
        # network (the firewall rule allows the private network only). Laptop: 127.0.0.1.
        cert, key = os.environ.get("KLINIK_TLS_CERT"), os.environ.get("KLINIK_TLS_KEY")
        uvicorn.run(
            "klinik_local.main:create_app",
            factory=True,
            host=os.environ.get("KLINIK_BIND", "127.0.0.1"),
            port=int(os.environ.get("KLINIK_PORT", "8443")),
            ssl_certfile=cert or None,
            ssl_keyfile=key or None,
            proxy_headers=False,
            server_header=False,
            log_level="info",
        )
    elif args.target == "jobs":
        import logging

        from klinik_local.jobs.__main__ import scheduler
        from klinik_local.jobs.config import JobSettings

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(scheduler(JobSettings()))
    elif args.target == "migrate":
        migrate()
    elif args.target == "manage":
        from klinik_local import manage

        sys.argv = ["klinik_local.manage", *args.rest]
        manage.main()
    elif args.target == "job":
        from klinik_local.jobs.__main__ import main as jobs_main

        sys.argv = ["klinik_local.jobs", *args.rest]
        jobs_main()


if __name__ == "__main__":
    main()
