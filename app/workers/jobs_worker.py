"""Jobs-worker (§2.1, §3.2): обработка AI-заданий с lease и лимитами.

- выборка через SELECT FOR UPDATE SKIP LOCKED + lease 120 с, продление
  каждые 20 с (heartbeat); просроченный lease возвращается в очередь, номер
  попытки увеличивается;
- очередь AI до 60 секунд, затем запасной сценарий (§1.3);
- одновременных LLM-вызовов не более concurrent_llm (остальные задания в
  очереди); допустимое число AI-вызовов остаётся глобальным при масштабиро-
  вании worker (§2.1) — в пределах процесса лимит задаёт семафор, суточный
  денежный лимит контролируется транзакционно в БД;
- два worker не могут применить один результат: уникальное задание,
  lease_owner и версия проверяются при commit (§3.2);
- финансовый резерв записывается до внешнего вызова (§3.2).

Часть 3 обрабатывает kind='assess'; report/transcribe/tts остаются pending
до частей 4–5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime

import asyncpg

from app.services import assess_service, metrics, report_service, voice_service
from app.storage import jobs_repo
from app.workers.dispatch import WorkerDeps
from contracts.models import ErrorCode

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5


class JobsWorker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        deps: WorkerDeps,
        *,
        kinds: tuple[str, ...] = ("assess", "report", "transcribe", "tts"),
        llm_slots: asyncio.Semaphore | None = None,
        stt_slots: asyncio.Semaphore | None = None,
        tts_slots: asyncio.Semaphore | None = None,
    ) -> None:
        self.pool = pool
        self.deps = deps
        self.kinds = kinds
        self.owner_id = f"jobs-{os.getpid()}"
        limits = deps.config.settings.limits
        # расчётные лимиты параллельности: 5 LLM, 2 STT, 2 TTS (§1.3)
        self.llm_slots = llm_slots or asyncio.Semaphore(limits.concurrent_llm)
        self.stt_slots = stt_slots or asyncio.Semaphore(limits.concurrent_stt)
        self.tts_slots = tts_slots or asyncio.Semaphore(limits.concurrent_tts)
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Цикл обработки с ограниченным параллелизмом (§1.3): задания
        обрабатываются конкурентно, фактическая параллельность провайдеров
        ограничена семафорами 5 LLM / 2 STT / 2 TTS; прочие задания остаются
        в очереди БД (in-flight ограничен суммой слотов)."""
        logger.info("jobs worker started owner=%s kinds=%s", self.owner_id, self.kinds)
        inflight: set[asyncio.Task] = set()
        max_inflight = (
            self.deps.config.settings.limits.concurrent_llm
            + self.deps.config.settings.limits.concurrent_stt
            + self.deps.config.settings.limits.concurrent_tts
        )
        while not self._stop.is_set():
            if len(inflight) >= max_inflight:
                await asyncio.wait(
                    inflight, timeout=POLL_INTERVAL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            job = await self._claim()
            if job is None:
                if inflight:
                    await asyncio.wait(
                        inflight, timeout=POLL_INTERVAL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await self._sleep(POLL_INTERVAL_SECONDS)
                continue
            task = asyncio.create_task(self._process_guarded(job))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        logger.info("jobs worker stopped")

    async def _process_guarded(self, job: asyncpg.Record) -> None:
        """Изоляция задания: сбой одного не останавливает остальные."""
        try:
            await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job task crashed", extra={"job": str(job["id"])})

    async def _claim(self) -> asyncpg.Record | None:
        try:
            async with self.pool.acquire() as conn:
                recovered = await jobs_repo.recover_expired_leases(conn)
                if recovered:
                    metrics.REGISTRY.inc(metrics.JOB_LEASE_EXPIRED, value=recovered)
                    logger.warning(
                        "job leases expired, requeued", extra={"count": recovered}
                    )
                return await jobs_repo.claim_next(
                    conn,
                    kinds=self.kinds,
                    owner=self.owner_id,
                    lease_seconds=self.deps.config.settings.timeouts_seconds.worker_lease,
                )
        except (asyncpg.PostgresError, OSError) as exc:
            logger.warning("jobs claim failed", extra={"error": type(exc).__name__})
            await self._sleep(2.0)
            return None

    async def _process(self, job: asyncpg.Record) -> None:
        settings = self.deps.config.settings
        created = job["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - created).total_seconds()
        queue_budget = settings.limits.ai_queue_max_seconds - elapsed
        kind = job["kind"]

        if queue_budget <= 0:
            # Очередь AI превышена -> запасной сценарий без вызова (§1.3)
            logger.warning(
                "ai queue timeout, fallback",
                extra={"job": str(job["id"]), "kind": kind},
            )
            await self._queue_timeout_fallback(job, "queue_timeout")
            return

        slots = {
            "assess": self.llm_slots,
            "report": self.llm_slots,
            "transcribe": self.stt_slots,
            "tts": self.tts_slots,
        }.get(kind, self.llm_slots)

        renewal = asyncio.create_task(self._renew_loop(job["id"]))
        stop_loading = asyncio.Event()
        loading: asyncio.Task | None = None
        try:
            try:
                await asyncio.wait_for(slots.acquire(), timeout=queue_budget)
            except TimeoutError:
                logger.warning(
                    "ai slot wait timeout, fallback",
                    extra={"job": str(job["id"]), "kind": kind},
                )
                await self._queue_timeout_fallback(job, "slot_timeout")
                return
            try:
                # Статус ожидания обновляется один раз после порога (§3.7)
                chat_id = await self._chat_id_of(job)
                loading_key = self._loading_key(job)
                if chat_id is not None and loading_key is not None:
                    loading = asyncio.create_task(
                        assess_service.loading_update_task(
                            self.pool,
                            self.deps,
                            turn_id=job["turn_id"],
                            chat_id=chat_id,
                            stop=stop_loading,
                            unique_key=loading_key,
                        )
                    )
                await self._run_by_kind(job, kind)
                await self._record_job_metrics(job, kind, created)
            finally:
                slots.release()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "job crashed", extra={"job": str(job["id"]), "kind": kind}
            )
            await self._safe_release(job["id"])
        finally:
            stop_loading.set()
            if loading is not None:
                loading.cancel()
            renewal.cancel()

    @staticmethod
    def _loading_key(job: asyncpg.Record) -> str | None:
        if job["kind"] == "assess":
            return f"ack:{job['turn_id']}"
        if job["kind"] == "report":
            return f"ui:{job['session_id']}:report-started"
        return None

    async def _run_by_kind(self, job: asyncpg.Record, kind: str) -> None:
        if kind == "assess":
            await assess_service.run_assessment(self.pool, self.deps, job, self.owner_id)
        elif kind == "report":
            await report_service.process_report_job(
                self.pool, self.deps, job, self.owner_id
            )
        elif kind == "transcribe":
            await voice_service.process_transcribe_job(
                self.pool, self.deps, job, self.owner_id
            )
        elif kind == "tts":
            await voice_service.process_tts_job(self.pool, self.deps, job, self.owner_id)
        else:  # pragma: no cover - claim ограничен kinds
            logger.warning("unknown job kind", extra={"kind": kind})

    async def _record_job_metrics(
        self, job: asyncpg.Record, kind: str, created: datetime
    ) -> None:
        """Учёт обработки задания (§3.10): итоговое состояние и задержка
        этапа от создания до завершения; без содержимого."""
        try:
            async with self.pool.acquire() as conn:
                state = await conn.fetchval(
                    "SELECT state FROM jobs WHERE id = $1", job["id"]
                )
        except (asyncpg.PostgresError, OSError):  # pragma: no cover
            state = None
        metrics.REGISTRY.inc(metrics.JOB_PROCESSED, kind=kind, state=state or "unknown")
        metrics.REGISTRY.observe(
            metrics.LATENCY_JOB,
            (datetime.now(UTC) - created).total_seconds(),
            kind=kind,
        )

    async def _queue_timeout_fallback(self, job: asyncpg.Record, detail: str) -> None:
        kind = job["kind"]
        if kind == "assess":
            await assess_service.apply_direct_fallback(
                self.pool, self.deps, job, self.owner_id,
                ErrorCode.AI_UNAVAILABLE, detail,
            )
        elif kind == "report":
            # Отчёт без LLM: детерминированный запасной (degraded)
            await report_service.process_report_job(
                self.pool, self.deps, job, self.owner_id, force_deterministic=True
            )
        elif kind == "transcribe":
            await voice_service.fail_transcribe_job(
                self.pool, self.deps, job, self.owner_id, detail
            )
        elif kind == "tts":
            await voice_service.fail_tts_job(
                self.pool, self.deps, job, self.owner_id, detail
            )

    async def _chat_id_of(self, job: asyncpg.Record) -> int | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT u.chat_id FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.id = $1
                """,
                job["session_id"],
            )

    async def _renew_loop(self, job_id) -> None:
        interval = self.deps.config.settings.timeouts_seconds.worker_heartbeat
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.pool.acquire() as conn:
                    ok = await jobs_repo.renew_lease(
                        conn,
                        job_id,
                        owner=self.owner_id,
                        lease_seconds=self.deps.config.settings.timeouts_seconds.worker_lease,
                    )
                if not ok:
                    return
            except (asyncpg.PostgresError, OSError):  # pragma: no cover
                logger.warning("lease renewal failed", extra={"job": str(job_id)})

    async def _safe_release(self, job_id) -> None:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE jobs SET state = 'pending', lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE id = $1 AND state = 'leased' AND lease_owner = $2
                    """,
                    job_id,
                    self.owner_id,
                )
        except (asyncpg.PostgresError, OSError):  # pragma: no cover
            logger.warning("job release failed", extra={"job": str(job_id)})

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
