from __future__ import annotations

from fastapi.testclient import TestClient

from klinik_local.config import Settings
from klinik_local.main import create_app


def test_healthz_without_database() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "klinik-local", "db": "not_configured"}


def test_home_page_is_branded() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Nil Psikoloji" in response.text
    assert "Klinik Kasa" in response.text


def test_api_docs_are_not_exposed() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
