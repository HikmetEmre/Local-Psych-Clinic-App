"""Static checks of the office compose file (brief §11, §12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]


def test_the_database_publishes_no_port() -> None:
    assert "ports" not in SERVICES["local-db"]


def test_published_ports_are_bound_to_localhost_only() -> None:
    for name, service in SERVICES.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} exposes {port} beyond this computer"


def test_long_running_services_restart_by_themselves() -> None:
    for name in ("local-db", "local-app", "sync-worker", "backup"):
        assert SERVICES[name]["restart"] == "unless-stopped", name


def test_the_app_and_worker_do_not_use_the_owner_role() -> None:
    assert "kasa_app:" in SERVICES["local-app"]["environment"]["LOCAL_DATABASE_URL"]
    assert "kasa_sync:" in SERVICES["sync-worker"]["environment"]["SYNC_LOCAL_DATABASE_URL"]
    assert SERVICES["backup"]["environment"]["PGUSER"] == "kasa_backup"


def test_the_backup_job_gets_only_the_public_key() -> None:
    env = SERVICES["backup"]["environment"]
    assert "AGE_RECIPIENT" in env
    assert not any("IDENTITY" in key or "PRIVATE" in key for key in env)
    assert "LOCAL_FERNET_KEYS" not in env
