"""Worker-процесс модульного монолита (§2.1): команды, AI-задания, исходящие.

Обрабатывает inbox-обновления, outbox-доставку и AI-задания четырёх видов:
assess, report, transcribe, tts (расчётные лимиты параллельности 5/2/2, §1.3).

Часть 4 добавляет голосовой контур: при запуске выполняется startup cleanup
временного аудио (§3.5), фоново — sweep просроченных записей каждые 5 минут.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

from app.config import ConfigError, load_config
from app.providers.llm import build_llm_client
from app.providers.stt import build_stt_client
from app.providers.tts import build_tts_client
from app.services import lifecycle, media_service
from app.storage.db import Db, try_connect
from app.telegram.bot import AiogramBotClient
from app.workers.dispatch import WorkerDeps
from app.workers.inbox_worker import InboxWorker
from app.workers.jobs_worker import JobsWorker
from app.workers.outbox_worker import OutboxWorker, RateLimiter

logger = logging.getLogger(__name__)

DB_RETRY_SECONDS = 5.0


async def run_worker() -> None:
    config = load_config()
    stop = asyncio.Event()

    db: Db | None = await try_connect(config.env.database_url)
    while db is None and not stop.is_set():
        logger.warning("worker waiting for database…")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=DB_RETRY_SECONDS)
        db = await try_connect(config.env.database_url)
    if db is None:
        return

    bot = AiogramBotClient(config.env.bot_token)
    limiter = RateLimiter(
        global_per_second=config.settings.sender.global_per_second,
        per_chat_per_second=config.settings.sender.per_chat_per_second,
    )
    # LLM-клиент создаётся только при заполненном профиле провайдера (§1.4);
    # иначе AI-задания уходят в запасной сценарий
    llm = build_llm_client(config)
    if llm is None:
        logger.warning("llm client not created: AI fallback mode")
    stt = build_stt_client(config)
    tts = build_tts_client(config)
    if config.ai_enabled and (stt is None or tts is None):
        logger.warning("stt/tts client not created: voice fallback mode")
    deps = WorkerDeps(config=config, bot=bot, limiter=limiter, llm=llm, stt=stt, tts=tts)

    # После аварии процесса cleanup выполняется при запуске (§3.5):
    # все файлы временного каталога считаются сиротами
    media_service.ensure_media_dir(config.env.media_dir)
    removed = media_service.startup_cleanup(config.env.media_dir)
    if removed:
        logger.info("media startup cleanup removed=%d", removed)

    # Сверка с tombstones ДО приёма трафика (§3.9, AT13): сначала
    # восстановленный inbox удалённых профилей пропускается (пока user_id
    # ещё ссылается на профиль из tombstone), затем восстановленный из более
    # старого backup профиль удалённого пользователя удаляется повторно.
    try:
        async with db.pool.acquire() as conn:
            inbox_skipped = await lifecycle.purge_inbox_of_deleted_users(conn)
            re_deleted = await lifecycle.reapply_tombstones(conn)
        if re_deleted or inbox_skipped:
            logger.warning(
                "startup tombstone reconciliation",
                extra={"re_deleted": re_deleted, "inbox_skipped": inbox_skipped},
            )
    except Exception as exc:  # не блокируем запуск: sweep повторит
        logger.warning("startup tombstone reconciliation failed: %s", type(exc).__name__)

    inbox_worker = InboxWorker(db.pool, deps)
    outbox_worker = OutboxWorker(db.pool, deps)
    jobs_worker = JobsWorker(db.pool, deps)
    sweep_task = asyncio.create_task(_media_sweep_loop(config, db))
    heartbeat_task = asyncio.create_task(_heartbeat_loop(config))
    monitor_task = asyncio.create_task(_monitor_loop(config, db))
    session_sweep_task = asyncio.create_task(
        _lifecycle_loop(config, db, _run_session_sweep,
                        config.settings.retention.session_sweep_interval_minutes * 60,
                        "session sweep")
    )
    retention_sweep_task = asyncio.create_task(
        _lifecycle_loop(config, db, _run_retention_sweep,
                        config.settings.retention.retention_sweep_interval_minutes * 60,
                        "retention sweep")
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # pragma: no cover - не POSIX
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def _watch_signals() -> None:
        await stop.wait()
        inbox_worker.request_stop()
        outbox_worker.request_stop()
        jobs_worker.request_stop()

    logger.info(
        "worker started pid=%s ai=%s billing=%s",
        os.getpid(),
        "enabled" if config.ai_enabled else "disabled",
        config.settings.flags.billing_enabled,
    )
    try:
        await asyncio.gather(
            inbox_worker.run(),
            outbox_worker.run(),
            jobs_worker.run(),
            _watch_signals(),
            sweep_task,
            session_sweep_task,
            retention_sweep_task,
            heartbeat_task,
            monitor_task,
        )
    finally:
        sweep_task.cancel()
        session_sweep_task.cancel()
        retention_sweep_task.cancel()
        heartbeat_task.cancel()
        monitor_task.cancel()
        if llm is not None:
            await llm.close()
        if stt is not None:
            await stt.close()
        if tts is not None:
            await tts.close()
        await bot.close()
        await db.close()
        logger.info("worker stopped")


async def _media_sweep_loop(config, db: Db) -> None:
    """Фоновый sweep: удаление просроченного аудио каждые 5 минут и очистка
    media_storage_key завершённых заданий (§3.5, §3.9)."""
    retention = config.settings.retention
    interval = retention.audio_sweep_interval_minutes * 60
    max_age = retention.audio_max_hours * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            removed = await asyncio.to_thread(
                media_service.sweep_expired, config.env.media_dir, max_age
            )
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE jobs SET media_storage_key = NULL
                    WHERE media_storage_key IS NOT NULL
                      AND (state IN ('done', 'failed', 'cancelled')
                           OR created_at < now() - make_interval(hours => $1))
                    """,
                    retention.audio_max_hours,
                )
            if removed:
                logger.info("media sweep removed=%d", removed)
        except Exception as exc:  # sweep не должен ронять worker
            logger.warning("media sweep failed: %s", type(exc).__name__)


async def _heartbeat_loop(config) -> None:
    """Heartbeat worker-процесса в RUN_DIR (§3.10: сигнал при отсутствии
    более 60 с); интервал — worker_heartbeat (20 с), меньше порога сигнала."""
    from app.services import monitoring

    interval = config.settings.timeouts_seconds.worker_heartbeat
    while True:
        try:
            await asyncio.to_thread(monitoring.write_heartbeat, config.env.run_dir)
        except OSError:  # pragma: no cover - зависит от ФС
            logger.warning("heartbeat write failed")
        await asyncio.sleep(interval)


async def _monitor_loop(config, db: Db) -> None:
    """Периодический снимок метрик/очередей и сигналы оператору (§3.10).

    Раз в минуту: структурированный лог `metrics` (счётчики процесса +
    размеры и возраст очередей из БД, без содержимого ответов) и вычисление
    операторских сигналов; канал уведомлений задаётся перед запуском."""
    from app.services import metrics, monitoring

    state = monitoring.SignalState()
    while True:
        await asyncio.sleep(60)
        try:
            async with db.pool.acquire() as conn:
                gauges = await monitoring.collect_gauges(conn, config)
            snapshot = metrics.REGISTRY.snapshot()
            logger.info(
                "metrics %s",
                json.dumps({"gauges": gauges, **snapshot}, ensure_ascii=False, default=str),
            )
            signals = monitoring.evaluate_signals(gauges, config, state)
            monitoring.emit_signals(signals)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("monitor cycle failed: %s", type(exc).__name__)


async def _lifecycle_loop(config, db: Db, sweep, interval_seconds: float, label: str) -> None:
    """Общий цикл фоновых sweep-ов жизненного цикла (§3.9): первый проход
    сразу после запуска, далее по интервалу; сбой не роняет worker."""
    while True:
        try:
            await sweep(config, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s failed: %s", label, type(exc).__name__)
        await asyncio.sleep(interval_seconds)


async def _run_session_sweep(config, db: Db) -> dict:
    return await lifecycle.run_session_sweep(db.pool, config, config.env.media_dir)


async def _run_retention_sweep(config, db: Db) -> dict:
    return await lifecycle.run_retention_sweep(db.pool, config)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_worker())
    except ConfigError as exc:
        for problem in exc.problems:
            logger.error("config: %s", problem)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
