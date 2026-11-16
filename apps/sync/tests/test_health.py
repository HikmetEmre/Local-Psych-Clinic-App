from __future__ import annotations

from fastapi.testclient import TestClient

from klinik_sync.config import Settings
from klinik_sync.main import create_app


def test_healthz_without_database() -> None:
    with TestClient(create_app(Settings(local_database_url=None))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "klinik-sync", "db": "not_configured"}


def test_worker_serves_no_ui() -> None:
    with TestClient(create_app(Settings(local_database_url=None))) as client:
        assert client.get("/").status_code == 404
        assert client.get("/static/ui/css/klinik.css").status_code == 404
