"""Адаптер TTS-провайдера (§3.5; задача T17).

TTS получает только текст вопроса до 700 символов, без ответа пользователя.
GenerateTTSInput задаёт ru и ogg_opus; SynthesizedAudio возвращает внутренний
storage_key, MIME и длительность. Таймаут одного вызова 15 с, общий дедлайн
95 с (§3.2). Сбой озвучивания не блокирует ответ: вопрос уже доставлен текстом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class TtsError(Exception):
    pass


@dataclass(frozen=True)
class TtsRetryableError(TtsError):
    retry_after_seconds: int | None = None
    status: int | None = None


@dataclass(frozen=True)
class TtsFatalError(TtsError):
    status: int | None = None


@dataclass(frozen=True)
class TtsConfigError(TtsError):
    status: int | None = None


@dataclass(frozen=True)
class TtsUnknownResultError(TtsError):
    detail: str = ""


class TtsClient(Protocol):
    async def synthesize(self, text: str, dest_path: str) -> float:
        """Пишет OGG/Opus в dest_path; возвращает длительность в секундах."""
        ...

    async def close(self) -> None: ...


class HttpTtsClient:
    """OpenAI-совместимый audio/speech (response_format=opus)."""

    def __init__(self, *, endpoint: str, model: str, api_key: str, voice: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._voice = voice
        self._session = None

    async def _get_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def synthesize(self, text: str, dest_path: str) -> float:
        import aiohttp

        session = await self._get_session()
        url = f"{self._endpoint}/audio/speech"
        body = {
            "model": self._model,
            "input": text,
            "voice": self._voice,
            "response_format": "opus",
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                status = response.status
                if status != 200:
                    await response.read()
                    self._raise_for_status(status, response.headers.get("Retry-After"))
                total = 0
                with open(dest_path, "wb") as fh:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > 25 * 1024 * 1024:  # защита диска
                            raise TtsFatalError(status=200)
                        fh.write(chunk)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise TtsUnknownResultError(type(exc).__name__) from exc

        from app.services.media_service import probe_ogg_opus

        info = probe_ogg_opus(dest_path)
        if info is None:
            raise TtsFatalError(status=200)
        return info.duration_seconds

    def _raise_for_status(self, status: int, retry_after: str | None) -> None:
        if status == 429:
            seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
            raise TtsRetryableError(retry_after_seconds=seconds, status=429)
        if 500 <= status <= 599:
            raise TtsRetryableError(status=status)
        if status in (401, 403):
            logger.error("tts auth failure", extra={"status": status})
            raise TtsConfigError(status=status)
        raise TtsFatalError(status=status)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def build_tts_client(config) -> TtsClient | None:
    import os

    if not config.ai_enabled or config.provider_profile is None:
        return None
    tts = config.provider_profile.tts
    api_key = os.environ.get(tts.api_key_env or "", "")
    if not api_key or not tts.endpoint or not tts.model:
        return None
    extra = tts.model_extra or {}
    voice = extra.get("voice") or "default"
    return HttpTtsClient(
        endpoint=tts.endpoint, model=tts.model, api_key=api_key, voice=str(voice)
    )
