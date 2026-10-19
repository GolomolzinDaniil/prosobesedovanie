"""Inbox-worker (§2.2, §3.2): транзакционный обработчик обновлений.

- выборка через SELECT FOR UPDATE SKIP LOCKED + lease (120 с);
- обработчик повторяется до 5 раз, не повторяя уже сохранённые доменные
  эффекты (unique_key outbox/jobs/events + дедупликация update_id);
- фаза 1 (вне транзакции): быстрый ack callback и групповое уведомление;
- фаза 2 (транзакция): доменная обработка и mark_processed;
- сбой -> requeue с backoff; просроченные lease возвращаются в очередь.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import asyncpg

from app.services import metrics
from app.storage import inbox_repo
from app.workers.dispatch import WorkerDeps, handle_update, pre_process

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.3
LEASE_RECOVER_INTERVAL_SECONDS = 30.0


class InboxWorker:
    def __init__(self, pool: asyncpg.Pool, deps: WorkerDeps) -> None:
        self.pool = pool
        self.deps = deps
        self.owner_id = f"inbox-{os.getpid()}"
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("inbox worker started owner=%s", self.owner_id)
        last_recover = 0.0
        while not self._stop.is_set():
            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_recover > LEASE_RECOVER_INTERVAL_SECONDS:
                last_recover = loop_now
                await self._recover_leases()
            item = await self._claim_one()
            if item is None:
                await self._sleep(POLL_INTERVAL_SECONDS)
                continue
            await self._process(item)
        logger.info("inbox worker stopped")

    async def _recover_leases(self) -> None:
        try:
            async with self.pool.acquire() as conn:
                released = await inbox_repo.release_expired_leases(conn)
                if released:
                    metrics.REGISTRY.inc(metrics.INBOX_LEASE_EXPIRED, value=released)
                    logger.info("released expired inbox leases", extra={"count": released})
        except (asyncpg.PostgresError, OSError) as exc:
            logger.warning("lease recovery failed", extra={"error": type(exc).__name__})

    async def _claim_one(self) -> asyncpg.Record | None:
        try:
            async with self.pool.acquire() as conn:
                batch = await inbox_repo.claim_batch(
                    conn,
                    owner=self.owner_id,
                    limit=1,
                    lease_seconds=self.deps.config.settings.timeouts_seconds.worker_lease,
                )
                return batch[0] if batch else None
        except (asyncpg.PostgresError, OSError) as exc:
            logger.warning("inbox claim failed", extra={"error": type(exc).__name__})
            await self._sleep(2.0)
            return None

    async def _process(self, item: asyncpg.Record) -> None:
        from app.domain.errors import DomainError
        from app.workers.dispatch import notify_domain_error

        update_id = item["update_id"]

        # Восстановленный inbox сверяется с tombstones до обработки (§3.9):
        # обновления профиля, удалённого по /delete, пропускаются без эффектов,
        # чтобы restore более старого backup не вернул удалённые данные.
        if item["user_id"] is not None and await self._is_tombstoned(item["user_id"]):
            logger.info(
                "inbox update skipped: user tombstoned", extra={"update_id": update_id}
            )
            async with self.pool.acquire() as conn:
                await inbox_repo.mark_skipped(conn, update_id)
            metrics.REGISTRY.inc(metrics.INBOX_PROCESSED, status="skipped")
            return

        try:
            await pre_process(self.deps, item)
        except Exception:
            logger.warning("pre_process error", extra={"update_id": update_id})

        domain_error: DomainError | None = None
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await handle_update(conn, self.deps, item)
                await inbox_repo.mark_processed(conn, update_id)
            self._record_processed(item)
        except DomainError as err:
            # Ожидаемая доменная ошибка: транзакция откатилась (частичные
            # эффекты не сохранены); сообщение пользователю — в новой транзакции
            domain_error = err
        except asyncio.CancelledError:
            await self._safe_release(item)
            raise
        except Exception as exc:
            logger.warning(
                "inbox handler failed, will retry",
                extra={
                    "update_id": update_id,
                    "attempts": item["attempts"],
                    "error": type(exc).__name__,
                },
                exc_info=True,
            )
            backoff = min(60, 5 * max(1, item["attempts"]))
            await self._requeue(update_id, error=f"{type(exc).__name__}", backoff=backoff)
            return

        if domain_error is not None:
            try:
                async with self.pool.acquire() as conn, conn.transaction():
                    await notify_domain_error(conn, self.deps, item, domain_error)
                    await inbox_repo.mark_processed(conn, update_id)
                self._record_processed(item)
            except Exception:
                logger.exception(
                    "domain error notification failed", extra={"update_id": update_id}
                )
                await self._requeue(update_id, error="notify_failed", backoff=5)

    @staticmethod
    def _record_processed(item: asyncpg.Record) -> None:
        """Обработка inbox учтена (§3.10); задержка — от приёма обновления."""
        metrics.REGISTRY.inc(metrics.INBOX_PROCESSED, status="processed")
        received = item["received_at"]
        if received is not None:
            from datetime import UTC, datetime

            if received.tzinfo is None:
                received = received.replace(tzinfo=UTC)
            metrics.REGISTRY.observe(
                metrics.LATENCY_INBOX, (datetime.now(UTC) - received).total_seconds()
            )

    async def _is_tombstoned(self, user_id) -> bool:
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT 1 FROM deletion_tombstones WHERE user_id = $1", user_id
                )
            return row is not None
        except (asyncpg.PostgresError, OSError):  # pragma: no cover
            logger.warning("tombstone check failed", extra={"error": "db"})
            return False

    async def _requeue(self, update_id: int, *, error: str, backoff: int) -> None:
        try:
            async with self.pool.acquire() as conn:
                status = await inbox_repo.requeue(
                    conn,
                    update_id,
                    error=error,
                    backoff_seconds=backoff,
                    max_retries=self.deps.config.settings.inbox.max_handler_retries,
                )
                metrics.REGISTRY.inc(
                    metrics.INBOX_PROCESSED, status="failed" if status == "failed" else "requeued"
                )
                if status == "failed":
                    # Сигнал оператору: обновление не обработано (§3.10)
                    logger.error(
                        "inbox update permanently failed",
                        extra={"update_id": update_id, "error": error},
                    )
        except (asyncpg.PostgresError, OSError):
            logger.exception("requeue failed; lease expiry will recover the row")

    async def _safe_release(self, item: asyncpg.Record) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE inbox
                    SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
                    WHERE update_id = $1 AND status = 'processing'
                    """,
                    item["update_id"],
                )
        except (asyncpg.PostgresError, OSError):  # pragma: no cover
            logger.warning("release on cancel failed")

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
