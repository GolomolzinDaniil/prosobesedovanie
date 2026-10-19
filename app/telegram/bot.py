"""Адаптер Bot API (§1.4, §2.1, §3.2): отдельный sender-порт и реализация aiogram.

Классификация исходов отправки:
- success: message_id подтверждён -> сохраняем в outbox.message_id/receipts;
- retry_after: 429 -> повтор с паузой retry_after (respect_retry_after);
- retryable: сетевая ошибка/5xx -> повтор с backoff (<=10 повторов в 24 ч);
- chat_unreachable: Telegram 403 -> чат недоступен, бесконечные повторы
  запрещены;
- bad_request: 400 с некорректным payload -> ошибка реализации, не повторяется;
- config_error: 401/403 на уровне бота -> сигнал оператору (§3.10).

Скорость ограничивает outbox-worker: одна отправка в секунду на чат, до 20
отправок в секунду суммарно как консервативная настройка (§1.4). Неизвестный
результат sendMessage допускает повторное сообщение: ровно одну доставку
гарантировать невозможно (§2.4); receipts содержат только известные
подтверждения.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SendOutcome(StrEnum):
    success = "success"
    retry_after = "retry_after"          # 429: retry_after секунд
    retryable = "retryable"              # сеть/5xx: backoff-повтор
    chat_unreachable = "chat_unreachable"  # 403: чат недоступен (терминально)
    bad_request = "bad_request"          # 400: ошибка реализации (терминально)
    config_error = "config_error"        # 401: неверный токен (терминально + сигнал)


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    message_id: int | None = None
    retry_after_seconds: int | None = None
    error: str | None = None


class DownloadOutcome(StrEnum):
    success = "success"
    too_large = "too_large"        # поток превысил предел -> VOICE_TOO_LARGE
    not_found = "not_found"        # getFile не дал путь
    retryable = "retryable"        # сеть/5xx
    fatal = "fatal"                # 400/401/403


@dataclass(frozen=True)
class DownloadResult:
    outcome: DownloadOutcome
    size_bytes: int = 0
    error: str | None = None


class BotApiClient(Protocol):
    """Порт внешнего Telegram-адаптера: только отправка, без домена и БД."""

    async def send_message(self, payload: dict[str, Any]) -> SendResult:
        """payload — параметры sendMessage в формате Bot API (без parse_mode:
        сервер форматирует сообщения в plain text, §3.4)."""
        ...

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        """Быстрый ack callback без ожидания AI (§3.1). Ошибки не критичны."""
        ...

    async def send_voice(self, payload: dict[str, Any]) -> SendResult:
        """payload: chat_id, voice_path, reply_to_message_id — озвучка вопроса,
        дополнение к тексту (§3.5)."""
        ...

    async def download_file(
        self, file_id: str, dest_path: str, max_bytes: int
    ) -> DownloadResult:
        """Скачивание только через Telegram getFile с жёстким пределом потока.
        File URL с токеном бота никогда не логируется (§3.5)."""
        ...

    async def close(self) -> None: ...


class AiogramBotClient:
    """Реализация порта на aiogram (S3). Вызовы — обычные, не сетевые для
    домена: домен/sender не обращается к Bot API напрямую (§3.10).

    api_base — необязательный альтернативный базовый URL Bot API (локальный
    сервер Bot API или тестовый стаб); по умолчанию api.telegram.org.
    """

    def __init__(self, bot_token: str, api_base: str | None = None) -> None:
        from aiogram import Bot

        if api_base:
            from aiogram.client.session.aiohttp import AiohttpSession
            from aiogram.client.telegram import TelegramAPIServer

            session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
            self._bot = Bot(token=bot_token, session=session)
        else:
            self._bot = Bot(token=bot_token)

    async def send_message(self, payload: dict[str, Any]) -> SendResult:
        from aiogram.exceptions import (
            TelegramAPIError,
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNetworkError,
            TelegramRetryAfter,
            TelegramServerError,
            TelegramUnauthorizedError,
        )

        params = dict(payload)
        params.pop("method", None)
        try:
            message = await self._bot.send_message(**params)
            return SendResult(SendOutcome.success, message_id=message.message_id)
        except TelegramRetryAfter as exc:
            return SendResult(
                SendOutcome.retry_after,
                retry_after_seconds=int(exc.retry_after),
                error="429",
            )
        except TelegramUnauthorizedError as exc:
            logger.error("bot token rejected (401)")
            return SendResult(SendOutcome.config_error, error=f"401:{type(exc).__name__}")
        except TelegramForbiddenError as exc:
            # 403: чат недоступен (бот заблокирован/пользователь закрыл чат)
            return SendResult(SendOutcome.chat_unreachable, error=f"403:{type(exc).__name__}")
        except TelegramBadRequest as exc:
            # 400 с некорректным payload — ошибка реализации, не повторяется
            return SendResult(SendOutcome.bad_request, error=f"400:{type(exc).__name__}")
        except (TelegramServerError, TelegramNetworkError) as exc:
            return SendResult(SendOutcome.retryable, error=f"{type(exc).__name__}")
        except (OSError, TimeoutError) as exc:
            # Неизвестный результат: сообщение могло быть доставлено (§2.4)
            return SendResult(SendOutcome.retryable, error=f"{type(exc).__name__}")
        except TelegramAPIError as exc:  # прочие ошибки API — ограниченно повторяем
            return SendResult(SendOutcome.retryable, error=f"{type(exc).__name__}")

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        from aiogram.exceptions import TelegramAPIError

        try:
            await self._bot.answer_callback_query(
                callback_query_id=callback_query_id, text=text, show_alert=show_alert
            )
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            # ack не критичен: не блокируем обработку (§3.1)
            logger.warning(
                "answerCallbackQuery failed", extra={"error": type(exc).__name__}
            )

    async def send_voice(self, payload: dict[str, Any]) -> SendResult:
        from aiogram.exceptions import (
            TelegramAPIError,
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNetworkError,
            TelegramRetryAfter,
            TelegramServerError,
            TelegramUnauthorizedError,
        )
        from aiogram.types import FSInputFile

        voice_file = FSInputFile(payload["voice_path"], filename="question.ogg")
        kwargs: dict[str, Any] = {"chat_id": payload["chat_id"], "voice": voice_file}
        reply_to = payload.get("reply_to_message_id")
        if reply_to:
            from aiogram.types import ReplyParameters

            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to, allow_sending_without_reply=True
            )
        try:
            message = await self._bot.send_voice(**kwargs)
            return SendResult(SendOutcome.success, message_id=message.message_id)
        except TelegramRetryAfter as exc:
            return SendResult(
                SendOutcome.retry_after,
                retry_after_seconds=int(exc.retry_after),
                error="429",
            )
        except TelegramUnauthorizedError:
            return SendResult(SendOutcome.config_error, error="401")
        except TelegramForbiddenError:
            return SendResult(SendOutcome.chat_unreachable, error="403")
        except (TelegramBadRequest, FileNotFoundError) as exc:
            return SendResult(SendOutcome.bad_request, error=type(exc).__name__)
        except (TelegramServerError, TelegramNetworkError, TelegramAPIError,
                OSError, TimeoutError) as exc:
            return SendResult(SendOutcome.retryable, error=type(exc).__name__)

    async def download_file(
        self, file_id: str, dest_path: str, max_bytes: int
    ) -> DownloadResult:
        from aiogram.exceptions import (
            TelegramAPIError,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramUnauthorizedError,
        )

        try:
            file = await self._bot.get_file(file_id)
        except (TelegramNotFound, TelegramForbiddenError, TelegramUnauthorizedError) as exc:
            return DownloadResult(DownloadOutcome.not_found, error=type(exc).__name__)
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            return DownloadResult(DownloadOutcome.retryable, error=type(exc).__name__)
        file_path = getattr(file, "file_path", None)
        if not file_path:
            return DownloadResult(DownloadOutcome.not_found, error="no_file_path")

        # URL содержит токен бота: не логируется и не передаётся наружу (§3.5)
        url = self._bot.session.api.file_url(token=self._bot.token, path=file_path)
        import aiohttp

        written = 0
        try:
            async with (
                aiohttp.ClientSession() as http,
                http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp,
            ):
                if resp.status != 200:
                    if resp.status in (401, 403):
                        return DownloadResult(DownloadOutcome.fatal, error=str(resp.status))
                    if resp.status == 404:
                        return DownloadResult(DownloadOutcome.not_found, error="404")
                    return DownloadResult(DownloadOutcome.retryable, error=str(resp.status))
                with open(dest_path, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            # Поток ограничен ДО полного скачивания (§3.5)
                            fh.close()
                            return DownloadResult(
                                DownloadOutcome.too_large, size_bytes=written
                            )
                        fh.write(chunk)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            return DownloadResult(DownloadOutcome.retryable, error=type(exc).__name__)
        return DownloadResult(DownloadOutcome.success, size_bytes=written)

    async def close(self) -> None:
        await self._bot.session.close()
