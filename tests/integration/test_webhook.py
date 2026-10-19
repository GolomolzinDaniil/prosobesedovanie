"""Webhook-протокол (§3.1): секрет, лимиты, дедупликация, коды ответов, 503."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from contracts.models import ErrorResponse
from tests.conftest import (
    WEBHOOK_HEADERS,
    WEBHOOK_PATH,
    make_callback_update,
    make_env,
    make_update,
)


async def test_valid_message_stored_pending(api_client, db) -> None:
    client, _app = api_client
    update = make_update(1001)
    r = await client.post(WEBHOOK_PATH, json=update, headers=WEBHOOK_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    row = await db.fetchrow("SELECT * FROM inbox WHERE update_id = 1001")
    assert row is not None
    assert row["kind"] == "message"
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert json.loads(row["payload"]) == update


async def test_duplicate_update_id_acked_once(api_client, db) -> None:
    """Повтор update_id получает 200 без повторной команды (§2.4, T06)."""
    client, _app = api_client
    update = make_update(1002)
    for _ in range(10):
        r = await client.post(WEBHOOK_PATH, json=update, headers=WEBHOOK_HEADERS)
        assert r.status_code == 200
        assert r.json() == {"ok": True}
    count = await db.fetchval("SELECT count(*) FROM inbox WHERE update_id = 1002")
    assert count == 1


async def test_callback_query_stored(api_client, db) -> None:
    client, _app = api_client
    r = await client.post(WEBHOOK_PATH, json=make_callback_update(2002), headers=WEBHOOK_HEADERS)
    assert r.status_code == 200
    row = await db.fetchrow("SELECT * FROM inbox WHERE update_id = 2002")
    assert row["kind"] == "callback_query"
    assert row["status"] == "pending"


async def test_unsupported_kind_skipped_without_ai(api_client, db) -> None:
    """Неподдерживаемый вид признан обработанным без вызова AI (§3.1)."""
    client, _app = api_client
    update = {
        "update_id": 3003,
        "edited_message": {"message_id": 1, "date": 1, "chat": {"id": 5, "type": "private"}},
    }
    r = await client.post(WEBHOOK_PATH, json=update, headers=WEBHOOK_HEADERS)
    assert r.status_code == 200
    row = await db.fetchrow("SELECT * FROM inbox WHERE update_id = 3003")
    assert row["kind"] == "unsupported:edited_message"
    assert row["status"] == "skipped"
    assert row["processed_at"] is not None


async def test_pre_checkout_skipped_while_billing_disabled(api_client, db) -> None:
    client, _app = api_client
    update = {
        "update_id": 3004,
        "pre_checkout_query": {
            "id": "pcq",
            "from": {"id": 555},
            "currency": "XTR",
            "total_amount": 75,
            "invoice_payload": "ord_x",
        },
    }
    r = await client.post(WEBHOOK_PATH, json=update, headers=WEBHOOK_HEADERS)
    assert r.status_code == 200
    row = await db.fetchrow("SELECT * FROM inbox WHERE update_id = 3004")
    assert row["kind"] == "pre_checkout_query"
    assert row["status"] == "skipped"


async def test_wrong_secret_403(api_client, db) -> None:
    client, _app = api_client
    headers = {**WEBHOOK_HEADERS, "X-Telegram-Bot-Api-Secret-Token": "wrong-secret-value-000"}
    r = await client.post(WEBHOOK_PATH, json=make_update(4001), headers=headers)
    assert r.status_code == 403
    body = ErrorResponse.model_validate(r.json())
    assert body.error.code.value == "FORBIDDEN"
    assert body.error.retryable is False
    assert await db.fetchval("SELECT count(*) FROM inbox WHERE update_id = 4001") == 0


async def test_missing_secret_header_403(api_client) -> None:
    client, _app = api_client
    r = await client.post(
        WEBHOOK_PATH, json=make_update(4002), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 403


async def test_secret_comparison_case_sensitive(api_client) -> None:
    client, _app = api_client
    headers = {**WEBHOOK_HEADERS, "X-Telegram-Bot-Api-Secret-Token": "S3CR3T-WEBHOOK-VALUE-0001"}
    r = await client.post(WEBHOOK_PATH, json=make_update(4003), headers=headers)
    assert r.status_code == 403


async def test_wrong_route_token_404(api_client, db) -> None:
    client, _app = api_client
    r = await client.post(
        "/telegram/webhook/wrong-route-token-999", json=make_update(4004), headers=WEBHOOK_HEADERS
    )
    assert r.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM inbox WHERE update_id = 4004") == 0


async def test_non_json_content_type_415(api_client) -> None:
    client, _app = api_client
    r = await client.post(
        WEBHOOK_PATH,
        content=b"{}",
        headers={**WEBHOOK_HEADERS, "Content-Type": "text/plain"},
    )
    assert r.status_code == 415
    body = ErrorResponse.model_validate(r.json())
    assert body.error.code.value == "UNSUPPORTED_MEDIA_TYPE"


async def test_json_with_charset_accepted(api_client) -> None:
    client, _app = api_client
    r = await client.post(
        WEBHOOK_PATH,
        content=json.dumps(make_update(4005)).encode(),
        headers={**WEBHOOK_HEADERS, "Content-Type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 200


async def test_oversized_body_413(api_client) -> None:
    client, _app = api_client
    big = make_update(4006, text="x" * (1024 * 1024 + 100))
    r = await client.post(WEBHOOK_PATH, json=big, headers=WEBHOOK_HEADERS)
    assert r.status_code == 413
    body = ErrorResponse.model_validate(r.json())
    assert body.error.code.value == "PAYLOAD_TOO_LARGE"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"",
        b"[1,2,3]",
        b'{"message": {}}',                       # нет update_id
        b'{"update_id": "abc"}',                  # update_id строкой
        b'{"update_id": 7, "message": {"message_id": 1, "date": 1}}',  # нет chat
    ],
)
async def test_invalid_structure_400(api_client, db, payload: bytes) -> None:
    client, _app = api_client
    r = await client.post(WEBHOOK_PATH, content=payload, headers=WEBHOOK_HEADERS)
    assert r.status_code == 400
    body = ErrorResponse.model_validate(r.json())
    assert body.error.code.value == "INVALID_UPDATE"


async def test_error_body_shape_and_request_id(api_client) -> None:
    """Единый ErrorResponse: код, сообщение, retryable, request_id (§3.2)."""
    client, _app = api_client
    r = await client.post(WEBHOOK_PATH, content=b"{}", headers=WEBHOOK_HEADERS)
    assert r.status_code == 400
    body = ErrorResponse.model_validate(r.json())
    uuid.UUID(str(body.request_id))  # валидный UUID
    assert body.ok is False
    assert body.error.message
    assert r.headers.get("X-Request-Id")


async def test_db_unavailable_503(db_url: str, db) -> None:
    """PostgreSQL недоступен -> 503, чтобы Telegram повторил доставку (§3.1)."""
    broken_env = make_env(DATABASE_URL="postgresql://app:app_dev_password@127.0.0.1:1/none")
    app = create_app(broken_env)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post(WEBHOOK_PATH, json=make_update(5001), headers=WEBHOOK_HEADERS)
            assert r.status_code == 503
            body = ErrorResponse.model_validate(r.json())
            assert body.error.code.value == "SERVICE_UNAVAILABLE"
            assert body.error.retryable is True
            assert body.error.retry_after_seconds is not None


async def test_no_secrets_in_error_bodies(api_client) -> None:
    """Контекст провайдера, трассировка и секреты в сообщение не попадают (§3.2)."""
    client, app = api_client
    config = app.state.config
    for payload, headers in (
        (b"{}", WEBHOOK_HEADERS),
        (
            json.dumps(make_update(6001)).encode(),
            {**WEBHOOK_HEADERS, "X-Telegram-Bot-Api-Secret-Token": "bad"},
        ),
    ):
        r = await client.post(WEBHOOK_PATH, content=payload, headers=headers)
        text = r.text
        assert config.env.bot_token not in text
        assert config.env.webhook_secret not in text
        assert config.env.database_url not in text
        assert "app_dev_password" not in text
