"""Адаптер LLM-провайдера (§1.4, §3.2, §3.3; задача T11).

Без подтверждённых модели, доступа, квот, региона и цены (provider-profile)
вызовы блокируются. Конкретный провайдер выбирается профилем; реализация —
OpenAI-совместимый HTTP endpoint (chat/completions), эквивалент документируется
в профиле (§3.3: если API не поддерживает температуру или native JSON Schema,
адаптер использует документированный эквивалент и строгую проверку JSON).

Правила повторов (§3.2):
- не более 2 вызовов провайдера на логическую операцию (контролирует
  assess_service через ai_calls UNIQUE(job_id, attempt));
- повтор при 429, 5xx и сетевом таймауте с паузой 2–5 секунд; retry_after
  учитывается только если укладывается в дедлайн (решает вызывающий код);
- 400/401/403 не повторяются — сигнал об ошибке конфигурации;
- неизвестный результат (таймаут после отправки) может быть тарифицирован:
  резерв стоимости не освобождается автоматически.

AI-провайдер не обращается напрямую к таблицам и Telegram (§3.10): адаптер
получает готовые промпты и возвращает текст модели (недоверенный результат).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LlmError(Exception):
    """Базовая ошибка LLM-вызова."""


@dataclass(frozen=True)
class LlmRetryableError(LlmError):
    """429/5xx: повтор допустим. retry_after_seconds — из ответа 429."""

    retry_after_seconds: int | None = None
    status: int | None = None


@dataclass(frozen=True)
class LlmFatalError(LlmError):
    """400: ошибка реализации/запроса — не повторяется."""

    status: int | None = None


@dataclass(frozen=True)
class LlmConfigError(LlmError):
    """401/403: ошибка конфигурации/доступа — сигнал оператору (§3.10)."""

    status: int | None = None


@dataclass(frozen=True)
class LlmUnknownResultError(LlmError):
    """Сетевой таймаут/обрыв после отправки: результат неизвестен,
    вызов мог быть тарифицирован (§3.2)."""

    detail: str = ""


@dataclass(frozen=True)
class LlmRequest:
    operation: str  # 'assess' | 'report'
    system_prompt: str
    user_payload: str  # компактный JSON входа (данные, не инструкции)
    max_output_tokens: int
    temperature: float
    timeout_seconds: int
    json_mode: bool = False


@dataclass(frozen=True)
class LlmResult:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_meta: dict[str, Any] = field(default_factory=dict)


class LlmClient(Protocol):
    """Порт LLM-провайдера."""

    async def complete(self, request: LlmRequest) -> LlmResult: ...

    async def close(self) -> None: ...


class HttpLlmClient:
    """OpenAI-совместимый клиент на aiohttp.

    endpoint из профиля — базовый URL (например https://api.provider.com/v1);
    запрос идёт на {endpoint}/chat/completions. Ключ — из переменной окружения
    api_key_env. Таймаут одного вызова задаёт вызывающий код (20 с оценка,
    35 с отчёт, §3.2).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        temperature_supported: bool = True,
        json_mode_supported: bool = False,
        max_context_tokens: int = 16000,
    ) -> None:
        import aiohttp

        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._temperature_supported = temperature_supported
        self._json_mode_supported = json_mode_supported
        self._max_context_tokens = max_context_tokens
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def complete(self, request: LlmRequest) -> LlmResult:
        import aiohttp

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_payload},
            ],
            "max_tokens": request.max_output_tokens,
        }
        if self._temperature_supported:
            body["temperature"] = request.temperature
        if request.json_mode and self._json_mode_supported:
            body["response_format"] = {"type": "json_object"}

        session = await self._get_session()
        url = f"{self._endpoint}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request.timeout_seconds),
            ) as response:
                raw = await response.text()
                status = response.status
                retry_after = response.headers.get("Retry-After")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # Неизвестный результат: запрос мог дойти и быть тарифицирован
            raise LlmUnknownResultError(f"{type(exc).__name__}") from exc

        if status == 200:
            return self._parse_success(raw)
        if status == 429:
            seconds = None
            try:
                seconds = int(retry_after) if retry_after else None
                if seconds is None:
                    data = json.loads(raw)
                    seconds = (data.get("parameters") or {}).get("retry_after")
                    seconds = int(seconds) if seconds is not None else None
            except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
                seconds = None
            raise LlmRetryableError(retry_after_seconds=seconds, status=429)
        if 500 <= status <= 599:
            raise LlmRetryableError(status=status)
        if status in (401, 403):
            logger.error("llm auth failure", extra={"status": status, "op": request.operation})
            raise LlmConfigError(status=status)
        if status == 400:
            raise LlmFatalError(status=400)
        # прочие 4xx — не повторяем (ошибка запроса/конфигурации)
        raise LlmFatalError(status=status)

    def _parse_success(self, raw: str) -> LlmResult:
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmFatalError(status=200) from exc
        if not isinstance(content, str):
            raise LlmFatalError(status=200)
        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return LlmResult(
            content=content,
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def build_llm_client(config) -> LlmClient | None:
    """Создаёт клиента по профилю провайдера; None если AI заблокирован (§1.4).

    Ключи берутся из окружения (api_key_env), значения профиля null блокируют
    запуск вызовов — разработчик не придумывает значения в коде.
    """
    import os

    if not config.ai_enabled or config.provider_profile is None:
        return None
    llm = config.provider_profile.llm
    api_key = os.environ.get(llm.api_key_env or "", "")
    if not api_key or not llm.endpoint or not llm.model:
        return None
    return HttpLlmClient(
        endpoint=llm.endpoint,
        model=llm.model,
        api_key=api_key,
        temperature_supported=bool(llm.temperature_supported),
        json_mode_supported=bool(llm.native_json_schema_supported),
        max_context_tokens=int(llm.context_window_tokens or 16000),
    )
