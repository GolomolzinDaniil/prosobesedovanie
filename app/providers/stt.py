"""Адаптер STT-провайдера (§1.4, §3.5; задача T16).

Таймаут одного вызова 30 с; общий дедлайн расшифровки 150 с с учётом
скачивания и повторов (§3.2). Ошибки классифицируются как в LLM-адаптере:
429/5xx/сеть — повтор (≤2 вызова на операцию), 400/401/403 — без повтора.
В БД не сохраняется исходная запись; файл и производные удаляются после
обработки (§3.5, §3.9).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class SttError(Exception):
    pass


@dataclass(frozen=True)
class SttRetryableError(SttError):
    retry_after_seconds: int | None = None
    status: int | None = None


@dataclass(frozen=True)
class SttFatalError(SttError):
    status: int | None = None


@dataclass(frozen=True)
class SttConfigError(SttError):
    status: int | None = None


@dataclass(frozen=True)
class SttUnknownResultError(SttError):
    detail: str = ""


class SttClient(Protocol):
    async def transcribe(self, file_path: str, *, language: str = "ru") -> str:
        """Возвращает распознанный текст (ненормированный — нормализует домен)."""
        ...

    async def close(self) -> None: ...


class HttpSttClient:
    """OpenAI-совместимый audio/transcriptions (multipart)."""

    def __init__(self, *, endpoint: str, model: str, api_key: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._session = None

    async def _get_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def transcribe(self, file_path: str, *, language: str = "ru") -> str:
        import aiohttp

        session = await self._get_session()
        url = f"{self._endpoint}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            form = aiohttp.FormData()
            form.add_field("model", self._model)
            form.add_field("language", language)
            form.add_file("file", file_path, content_type="audio/ogg")
            async with session.post(
                url,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                raw = await response.text()
                status = response.status
                retry_after = response.headers.get("Retry-After")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise SttUnknownResultError(type(exc).__name__) from exc

        if status == 200:
            try:
                payload = json.loads(raw)
                text = payload["text"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise SttFatalError(status=200) from exc
            if not isinstance(text, str):
                raise SttFatalError(status=200)
            return text
        if status == 429:
            seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
            raise SttRetryableError(retry_after_seconds=seconds, status=429)
        if 500 <= status <= 599:
            raise SttRetryableError(status=status)
        if status in (401, 403):
            logger.error("stt auth failure", extra={"status": status})
            raise SttConfigError(status=status)
        raise SttFatalError(status=status)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def build_stt_client(config) -> SttClient | None:
    import os

    if not config.ai_enabled or config.provider_profile is None:
        return None
    stt = config.provider_profile.stt
    api_key = os.environ.get(stt.api_key_env or "", "")
    if not api_key or not stt.endpoint or not stt.model:
        return None
    return HttpSttClient(endpoint=stt.endpoint, model=stt.model, api_key=api_key)
