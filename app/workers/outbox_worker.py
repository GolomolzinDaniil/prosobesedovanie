"""Outbox-sender (§1.4, §2.4, §3.2; задача T10).

- одна отправка в секунду на чат, до 20 отправок в секунду суммарно
  (консервативная настройка); при 429 соблюдается retry_after;
- повторы до 10 раз в пределах 24 часов с backoff;
- Telegram 403 — чат недоступен, 400 — ошибка реализации: обе не повторяются
  бесконечно;
- порядок: следующий обязательный текст не отправляется, пока предыдущий не
  подтверждён; необязательные строки (озвучка) текст не задерживают;
- перед отправкой повторно проверяется признак удаления; при восстановлении
  не отправляется результат отменённой/удалённой сессии;
- неизвестный результат допускает повторное сообщение: ровно одна доставка
  недостижима, receipts содержат только известные подтверждения.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import asyncpg

from app.config import AppConfig
from app.services import metrics
from app.storage import outbox_repo
from app.telegram.bot import SendOutcome
from app.workers.dispatch import WorkerDeps

logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 0.4


class RateLimiter:
    """Одна отправка в секунду на чат, до 20 суммарно (§1.4)."""

    def __init__(self, *, global_per_second: int, per_chat_per_second: int) -> None:
        self._global_interval = 1.0 / max(1, global_per_second)
        self._chat_interval = 1.0 / max(1, per_chat_per_second)
        self._last_global = 0.0
        self._last_chat: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, chat_id: int | None = None) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_global = self._last_global + self._global_interval - now
            wait = max(0.0, wait_global)
            if chat_id is not None:
                last_chat = self._last_chat.get(chat_id, 0.0)
                wait = max(wait, last_chat + self._chat_interval - now)
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._last_global = now
            if chat_id is not None:
                self._last_chat[chat_id] = now
                # редкие чаты не должны накапливать память
                if len(self._last_chat) > 10000:  # pragma: no cover
                    self._last_chat = {
                        k: v for k, v in self._last_chat.items() if now - v < 60
                    }


class OutboxWorker:
    def __init__(self, pool: asyncpg.Pool, deps: WorkerDeps) -> None:
        self.pool = pool
        self.deps = deps
        self.config: AppConfig = deps.config
        self.limiter: RateLimiter = deps.limiter
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("outbox worker started")
        while not self._stop.is_set():
            row = await self._claim()
            if row is None:
                await self._sleep(IDLE_POLL_SECONDS)
                continue
            await self._deliver(row)
        logger.info("outbox worker stopped")

    async def _claim(self) -> asyncpg.Record | None:
        try:
            async with self.pool.acquire() as conn:
                return await outbox_repo.claim_next(conn)
        except (asyncpg.PostgresError, OSError) as exc:
            logger.warning("outbox claim failed", extra={"error": type(exc).__name__})
            await self._sleep(2.0)
            return None

    async def _deliver(self, row: asyncpg.Record) -> None:
        # Повторная проверка признака удаления и отмены перед отправкой (§2.2)
        if await self._must_skip(row):
            await self._cancel_row(row)
            return

        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            await self._fail_row(row, error="invalid_payload", chat_reachable=False)
            return

        # delivery_started_at ставится до первой попытки отправки отчёта (§2.4)
        if row["kind"] == "report_part" and row["session_id"] is not None:
            from app.services import report_service

            async with self.pool.acquire() as conn:
                await report_service.mark_delivery_started(conn, row["session_id"])

        await self.limiter.acquire(row["chat_id"])
        if row["kind"] == "question_voice":
            result = await self._send_voice(payload)
        else:
            result = await self.deps.bot.send_message(payload)

        settings = self.config.settings.sender
        now = datetime.now(UTC)
        if result.outcome is SendOutcome.success:
            self._record_sent(row, metrics.OUTCOME_SUCCESS)
            await self._on_success(row, message_id=result.message_id or 0)
            if row["kind"] == "question_voice":
                self._cleanup_voice(row)
            return
        if result.outcome is SendOutcome.retry_after:
            self._record_sent(row, metrics.OUTCOME_RATE_LIMITED)
            pause = timedelta(seconds=result.retry_after_seconds or 1)
            await self._retry_or_fail(
                row, next_attempt=now + pause, error=result.error or "429"
            )
            return
        if result.outcome is SendOutcome.retryable:
            self._record_sent(row, metrics.OUTCOME_SERVER_ERROR)
            backoff = timedelta(
                seconds=min(
                    settings.backoff_base_seconds * (2 ** max(0, row["attempts"] - 1)),
                    3600,
                )
            )
            await self._retry_or_fail(
                row, next_attempt=now + backoff, error=result.error or "retry"
            )
            return
        # chat_unreachable / bad_request / config_error — терминально (§3.2)
        self._record_sent(row, f"terminal_{result.outcome.value}")
        await self._fail_row(
            row,
            error=result.error or result.outcome.value,
            chat_reachable=result.outcome is SendOutcome.bad_request,
        )
        if row["kind"] == "question_voice":
            self._cleanup_voice(row)

    def _record_sent(self, row: asyncpg.Record, outcome: str) -> None:
        """Учёт доставки (§3.10): вид строки и исход, 429/5xx отдельно;
        задержка от создания до подтверждения; успех report-delivery."""
        metrics.REGISTRY.inc(metrics.OUTBOX_SENT, kind=row["kind"], outcome=outcome)
        if outcome == metrics.OUTCOME_SUCCESS:
            created = row["created_at"]
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                metrics.REGISTRY.observe(
                    metrics.LATENCY_OUTBOX,
                    (datetime.now(UTC) - created).total_seconds(),
                )
        if row["kind"] == "report_part" and outcome.startswith("terminal_"):
            metrics.REGISTRY.inc(metrics.REPORT_DELIVERED, outcome="failed")

    def _cleanup_voice(self, row) -> None:
        from app.services.media_service import remove_file

        payload = row["payload"]
        if isinstance(payload, str):
            import json as _json

            try:
                payload = _json.loads(payload)
            except ValueError:
                payload = {}
        if isinstance(payload, dict):
            remove_file(payload.get("voice_path"))

    async def _send_voice(self, payload: dict):
        """Озвучка вопроса: файл удаляется после доставки/терминального
        отказа (§3.5). Отсутствие файла — терминальная ошибка строки."""
        import os

        voice_path = payload.get("voice_path") or ""
        if not voice_path or not os.path.exists(voice_path):
            from app.telegram.bot import SendOutcome, SendResult

            return SendResult(SendOutcome.bad_request, error="voice_file_missing")
        return await self.deps.bot.send_voice(payload)

    async def _must_skip(self, row: asyncpg.Record) -> bool:
        try:
            async with self.pool.acquire() as conn:
                fresh = await outbox_repo.get_by_id(conn, row["id"])
                if fresh is None or fresh["state"] == "cancelled":
                    return True
                user = await conn.fetchrow(
                    "SELECT deletion_requested_at FROM users WHERE id = $1", fresh["user_id"]
                )
                if (
                    user is not None
                    and user["deletion_requested_at"] is not None
                    and fresh["created_at"] < user["deletion_requested_at"]
                ):
                    return True
                if fresh["session_id"] is not None:
                    session = await conn.fetchrow(
                        "SELECT status FROM sessions WHERE id = $1", fresh["session_id"]
                    )
                    # результат отменённой сессии не отправляется (§3.2);
                    # служебные подтверждения отмены/удаления имеют kind='text'
                    if (
                        session is not None
                        and session["status"] == "cancelled"
                        and fresh["kind"]
                        in ("question_text", "question_voice", "report_part", "buttons")
                    ):
                        return True
                return False
        except (asyncpg.PostgresError, OSError):  # pragma: no cover
            return False

    async def _cancel_row(self, row: asyncpg.Record) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET state = 'cancelled' WHERE id = $1", row["id"]
            )

    async def _lock_chain(
        self, conn: asyncpg.Connection, row: asyncpg.Record
    ) -> tuple[asyncpg.Record | None, asyncpg.Record | None, asyncpg.Record | None]:
        """Канонический порядок блокировок users -> sessions -> outbox (§2.3).

        Возвращает (fresh_outbox, user_locked, session_locked); fresh_outbox
        перечитывается ПОСЛЕ блокировок: параллельная отмена видна сразу.
        """
        from app.storage import sessions_repo, users_repo

        user = await users_repo.lock_by_id(conn, row["user_id"])
        session = None
        if row["session_id"] is not None:
            session = await sessions_repo.find_by_id(conn, row["session_id"], for_update=True)
        fresh = await outbox_repo.get_by_id(conn, row["id"])
        return fresh, user, session

    async def _on_success(self, row: asyncpg.Record, message_id: int) -> None:
        from app.services import session_service

        async with self.pool.acquire() as conn, conn.transaction():
            fresh, _user, session = await self._lock_chain(conn, row)
            if fresh is None or fresh["state"] == "cancelled":
                # сессия/профиль отменены во время отправки: эффекты не применяем
                return
            await outbox_repo.mark_delivered(conn, fresh["id"], message_id=message_id)
            await session_service.on_outbox_delivered_tx(
                conn, self.config, fresh, message_id, session_locked=session
            )

    async def _retry_or_fail(
        self, row: asyncpg.Record, *, next_attempt: datetime, error: str
    ) -> None:
        settings = self.config.settings.sender
        window = timedelta(hours=settings.retry_window_hours)
        exhausted = await outbox_repo.retry_deadline_exceeded(
            row, window=window, max_retries=settings.max_retries
        )
        if exhausted:
            await self._fail_row(row, error=error, chat_reachable=True)
            return
        async with self.pool.acquire() as conn:
            await outbox_repo.schedule_retry(
                conn, row["id"], next_attempt_at=next_attempt, error=error
            )

    async def _fail_row(self, row: asyncpg.Record, *, error: str, chat_reachable: bool) -> None:
        from app.services import session_service

        logger.warning(
            "outbox delivery failed terminally",
            extra={"kind": row["kind"], "error": error[:200]},
        )
        async with self.pool.acquire() as conn, conn.transaction():
            fresh, user, session = await self._lock_chain(conn, row)
            if fresh is None or fresh["state"] == "cancelled":
                return
            await outbox_repo.mark_failed(conn, fresh["id"], error=error)
            await session_service.on_outbox_failed_tx(
                conn,
                self.config,
                fresh,
                error,
                chat_reachable=chat_reachable,
                user_locked=user,
                session_locked=session,
            )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
