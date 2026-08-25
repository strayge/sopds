"""Smoke tests for the initial HTTP application."""

from fastapi.testclient import TestClient

from sopds.app import create_app
from sopds.config import AppConfig


def test_health_endpoint(app_config: AppConfig) -> None:
    with TestClient(create_app(app_config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_uses_server_rendered_template(app_config: AppConfig) -> None:
    with TestClient(create_app(app_config)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "INPX-backed catalog" in response.text
    assert 'hx-get="/health-fragment"' in response.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in response.text
    assert "unpkg.com" not in response.text


def test_vendored_htmx_is_served_locally(app_config: AppConfig) -> None:
    with TestClient(create_app(app_config)) as client:
        response = client.get("/static/vendor/htmx/htmx-2.0.10.min.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.text.startswith("var htmx=")
