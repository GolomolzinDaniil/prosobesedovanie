"""Health-эндпоинты (§3.1): live всегда, ready только с БД и конфигурацией."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from tests.conftest import make_env


async def test_live_always_200(api_client) -> None:
    client, _app = api_client
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_200_with_db(api_client) -> None:
    client, _app = api_client
    r = await client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"
    assert body["ai"] == "disabled"  # provider-profile не заполнен
    assert body["billing"] == "disabled"


async def test_ready_503_without_db() -> None:
    broken_env = make_env(DATABASE_URL="postgresql://app:app_dev_password@127.0.0.1:1/none")
    app = create_app(broken_env)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get("/health/ready")
        assert r.status_code == 503
        assert "database_unavailable" in r.json()["reasons"]
        # live остаётся 200: процесс жив
        assert (await client.get("/health/live")).status_code == 200


async def test_ready_does_not_leak_secrets_or_db_address(api_client) -> None:
    client, app = api_client
    config = app.state.config
    r = await client.get("/health/ready")
    text = r.text
    assert config.env.bot_token not in text
    assert config.env.webhook_secret not in text
    assert "app_dev_password" not in text
    assert "postgresql://" not in text


async def test_unknown_route_404_error_response(api_client) -> None:
    client, _app = api_client
    r = await client.get("/admin/secret")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body
