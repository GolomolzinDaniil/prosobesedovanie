"""AiogramBotClient: маппинг исходов Bot API на SendOutcome (§3.2).

Тестируется полный стек: payload -> aiogram (построение формы, разбор
ответа, исключения) -> HTTP -> локальный стаб Bot API. Проверяются: успех с
message_id, 429 c retry_after, 403 чат недоступен, 400 ошибка реализации,
401 ошибка конфигурации, 5xx и сеть — повторяемо; ack callback не критичен.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import pytest_asyncio
from aiohttp import web

from app.telegram.bot import AiogramBotClient, SendOutcome

TOKEN = "123456:TEST-abcdef-token"

PAYLOAD = {
    "method": "sendMessage",
    "chat_id": 4242,
    "text": "Основной вопрос 1 из 6\n\nРасскажите о себе.",
    "reply_markup": {"force_reply": True, "selective": True},
    "reply_parameters": {"message_id": 7, "allow_sending_without_reply": True},
}


def message_result(message_id: int = 99) -> dict[str, Any]:
    return {
        "ok": True,
        "result": {
            "message_id": message_id,
            "date": 1760000000,
            "chat": {"id": 4242, "type": "private"},
            "text": "x",
        },
    }


def fail_body(code: int, description: str, parameters: dict | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


class TelegramStub:
    """Локальный стаб Bot API: скриптованные ответы + журнал запросов."""

    def __init__(self) -> None:
        self.responses: deque[tuple[int, dict[str, Any]]] = deque()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.base = ""

    def queue(self, status: int, body: dict[str, Any]) -> None:
        self.responses.append((status, body))

    def default_body(self, method: str) -> dict[str, Any]:
        if method == "sendMessage":
            return message_result()
        return {"ok": True, "result": True}

    async def handler(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        form = await request.post()
        self.requests.append((method, dict(form)))
        if self.responses:
            status, body = self.responses.popleft()
        else:
            status, body = 200, self.default_body(method)
        return web.json_response(body, status=status)


@pytest_asyncio.fixture
async def stub():
    app = web.Application()
    stub = TelegramStub()
    app.router.add_post("/bot{token}/{method}", stub.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    stub.base = f"http://127.0.0.1:{port}"
    try:
        yield stub
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def client(stub: TelegramStub):
    client = AiogramBotClient(TOKEN, api_base=stub.base)
    try:
        yield client
    finally:
        await client.close()


async def test_success_returns_message_id_and_builds_form(
    client: AiogramBotClient, stub: TelegramStub
) -> None:
    stub.queue(200, message_result(1234))
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.success
    assert result.message_id == 1234

    method, form = stub.requests[-1]
    assert method == "sendMessage"
    assert form["chat_id"] == "4242"
    assert "Расскажите о себе" in form["text"]
    markup = json.loads(form["reply_markup"])
    assert markup == {"force_reply": True, "selective": True}
    reply_params = json.loads(form["reply_parameters"])
    assert reply_params["message_id"] == 7


async def test_429_maps_to_retry_after(client: AiogramBotClient, stub: TelegramStub) -> None:
    stub.queue(
        429, fail_body(429, "Too Many Requests: retry after 7", {"retry_after": 7})
    )
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.retry_after
    assert result.retry_after_seconds == 7


async def test_403_maps_to_chat_unreachable(
    client: AiogramBotClient, stub: TelegramStub
) -> None:
    stub.queue(403, fail_body(403, "Forbidden: bot was blocked by the user"))
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.chat_unreachable


async def test_400_maps_to_bad_request(client: AiogramBotClient, stub: TelegramStub) -> None:
    stub.queue(400, fail_body(400, "Bad Request: chat not found"))
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.bad_request


async def test_401_maps_to_config_error(client: AiogramBotClient, stub: TelegramStub) -> None:
    stub.queue(401, fail_body(401, "Unauthorized"))
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.config_error


async def test_5xx_maps_to_retryable(client: AiogramBotClient, stub: TelegramStub) -> None:
    stub.queue(500, fail_body(500, "Internal Server Error"))
    result = await client.send_message(PAYLOAD)
    assert result.outcome is SendOutcome.retryable


async def test_network_error_maps_to_retryable() -> None:
    """Недоступный endpoint: неизвестный результат повторяемо (§3.2)."""
    closed = AiogramBotClient(TOKEN, api_base="http://127.0.0.1:1")
    try:
        result = await closed.send_message(PAYLOAD)
        assert result.outcome is SendOutcome.retryable
    finally:
        await closed.close()


async def test_answer_callback_query_success(
    client: AiogramBotClient, stub: TelegramStub
) -> None:
    stub.queue(200, {"ok": True, "result": True})
    await client.answer_callback_query("cb-1", text="Готово", show_alert=True)
    method, form = stub.requests[-1]
    assert method == "answerCallbackQuery"
    assert form["callback_query_id"] == "cb-1"
    assert form["text"] == "Готово"
    assert form["show_alert"] == "true"  # aiogram сериализует bool в JSON-стиле


async def test_answer_callback_query_failure_is_silent(
    client: AiogramBotClient, stub: TelegramStub
) -> None:
    stub.queue(400, fail_body(400, "Bad Request: query is too old"))
    # исключение не нарушает обработку (§3.1: вызывается быстро, не критично)
    await client.answer_callback_query("cb-old")
