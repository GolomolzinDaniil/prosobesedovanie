"""HttpLlmClient: OpenAI-совместимый вызов и классификация ошибок (§3.2)."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

from app.providers.llm import (
    HttpLlmClient,
    LlmConfigError,
    LlmFatalError,
    LlmRequest,
    LlmResult,
    LlmRetryableError,
    LlmUnknownResultError,
)

REQUEST = LlmRequest(
    operation="assess",
    system_prompt="Системный промпт",
    user_payload='{"answer":"данные"}',
    max_output_tokens=1500,
    temperature=0.2,
    timeout_seconds=20,
    json_mode=True,
)


class LlmStub:
    def __init__(self) -> None:
        self.responses: deque[tuple[int, dict[str, Any], dict[str, str]]] = deque()
        self.requests: list[dict[str, Any]] = []
        self.base = ""

    def queue(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        self.responses.append((status, body, headers or {}))

    async def handler(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append({"path": request.path, "body": body,
                              "auth": request.headers.get("Authorization")})
        if self.responses:
            status, resp_body, headers = self.responses.popleft()
        else:
            status, resp_body, headers = 200, {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }, {}
        return web.json_response(resp_body, status=status, headers=headers)


@pytest_asyncio.fixture
async def stub():
    app = web.Application()
    stub = LlmStub()
    app.router.add_post("/v1/chat/completions", stub.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    stub.base = f"http://127.0.0.1:{port}/v1"
    try:
        yield stub
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def client(stub: LlmStub):
    client = HttpLlmClient(
        endpoint=stub.base,
        model="test-model",
        api_key="sk-test",
        temperature_supported=True,
        json_mode_supported=True,
    )
    try:
        yield client
    finally:
        await client.close()


async def test_success_returns_content_and_usage(client, stub) -> None:
    stub.queue(
        200,
        {
            "choices": [{"message": {"content": '{"turn_id": "x"}'}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 567},
        },
    )
    result = await client.complete(REQUEST)
    assert isinstance(result, LlmResult)
    assert result.content == '{"turn_id": "x"}'
    assert result.input_tokens == 1234
    assert result.output_tokens == 567

    sent = stub.requests[-1]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["auth"] == "Bearer sk-test"
    body = sent["body"]
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 1500
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # ответ пользователя сериализован данными в user message, не в системном (§3.3)
    assert "данные" not in body["messages"][0]["content"]
    assert "данные" in body["messages"][1]["content"]


async def test_429_with_retry_after_header(client, stub) -> None:
    stub.queue(429, {"error": "rate limit"}, headers={"Retry-After": "4"})
    with pytest.raises(LlmRetryableError) as exc_info:
        await client.complete(REQUEST)
    assert exc_info.value.retry_after_seconds == 4


async def test_429_with_body_parameters(client, stub) -> None:
    stub.queue(429, {"error": "rate", "parameters": {"retry_after": 9}})
    with pytest.raises(LlmRetryableError) as exc_info:
        await client.complete(REQUEST)
    assert exc_info.value.retry_after_seconds == 9


async def test_5xx_retryable(client, stub) -> None:
    stub.queue(503, {"error": "overloaded"})
    with pytest.raises(LlmRetryableError):
        await client.complete(REQUEST)


async def test_400_fatal_no_retry(client, stub) -> None:
    stub.queue(400, {"error": "bad request"})
    with pytest.raises(LlmFatalError):
        await client.complete(REQUEST)


async def test_401_config_error(client, stub) -> None:
    stub.queue(401, {"error": "invalid key"})
    with pytest.raises(LlmConfigError):
        await client.complete(REQUEST)


async def test_connection_refused_is_unknown_result() -> None:
    """Недоступный endpoint: результат неизвестен, резерв стоимости не
    освобождается автоматически (§3.2)."""
    client = HttpLlmClient(endpoint="http://127.0.0.1:1/v1", model="m", api_key="k")
    try:
        with pytest.raises(LlmUnknownResultError):
            await client.complete(REQUEST)
    finally:
        await client.close()


async def test_malformed_success_body_is_fatal(client, stub) -> None:
    stub.queue(200, {"unexpected": True})
    with pytest.raises(LlmFatalError):
        await client.complete(REQUEST)
