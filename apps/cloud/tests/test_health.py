from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from klinik_cloud.config import Settings
from klinik_cloud.main import create_app


def test_healthz_without_database() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "klinik-cloud", "db": "not_configured"}


def test_healthz_reports_unreachable_database_as_503() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        auth_fernet_key=Fernet.generate_key().decode(),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["db"] == "down"


def test_home_page_is_branded() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        response = client.get("/")
        css = client.get("/static/ui/css/klinik.css")
        logo = client.get("/static/ui/img/logo.jpeg")
    assert response.status_code == 200
    assert "Nil Psikoloji" in response.text
    assert "Klinik Web" in response.text
    assert css.status_code == 200 and "#356650" in css.text
    assert logo.status_code == 200


def test_api_docs_are_not_exposed() -> None:
    with TestClient(create_app(Settings(database_url=None))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_refuses_to_start_without_an_auth_key() -> None:
    import pytest

    from klinik_cloud.main import ConfigError

    with pytest.raises(ConfigError, match="CLOUD_AUTH_FERNET_KEY"):
        create_app(Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z"))
