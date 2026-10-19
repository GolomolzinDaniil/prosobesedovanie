"""API-процесс модульного монолита (§2.1).

Один контейнерный образ запускается в двух процессах: API принимает webhook и
обслуживает health; worker обрабатывает команды, AI-задания и исходящие
сообщения (часть 2). Между процессами — PostgreSQL.

API стартует даже при недоступном PostgreSQL: webhook возвращает 503, чтобы
Telegram повторил доставку; /health/ready сообщает о неготовности; фоновая
задача переподключается (§3.1).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors_handlers import RequestIdMiddleware, install_error_handlers
from app.api.routes import health, webhook
from app.config import AppConfig, ConfigError, load_config
from app.storage.db import Db, try_connect

logger = logging.getLogger(__name__)

_DB_RETRY_INTERVAL_SECONDS = 5.0


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Содержимое ответов, расшифровки и AI-payload в логи не попадают (§3.9):
    # access-лог uvicorn ограничен методом/путём/кодом без тел запросов.


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    env: dict[str, str] | None = getattr(app.state, "env_override", None)
    try:
        config: AppConfig = load_config(env)
    except ConfigError as exc:
        # Конфигурационная ошибка делает сервис неготовым и блокирует запуск (§3.10)
        for problem in exc.problems:
            logger.error("config: %s", problem)
        raise
    app.state.config = config
    app.state.db = None

    db = await try_connect(config.env.database_url)
    app.state.db = db
    if db is None:
        logger.warning("starting without database: webhook will answer 503 until it is up")

    stop_event = asyncio.Event()

    async def _db_watchdog() -> None:
        """Переподключение к PostgreSQL после старта/сбоя (§3.1: 503 -> повтор)."""
        while not stop_event.is_set():
            try:
                if app.state.db is None:
                    app.state.db = await try_connect(config.env.database_url)
                    if app.state.db is not None:
                        logger.info("database connected")
                elif not await app.state.db.ping():
                    logger.warning("database ping failed; keep pool, ready reports 503")
            except Exception:  # pragma: no cover - защита watchdog
                logger.exception("db watchdog error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_DB_RETRY_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    watchdog = asyncio.create_task(_db_watchdog())
    logger.info(
        "api started: env=%s ai=%s billing=%s voice=%s",
        config.env.env_name,
        "enabled" if config.ai_enabled else "disabled",
        config.settings.flags.billing_enabled,
        config.settings.flags.voice_enabled,
    )
    try:
        yield
    finally:
        stop_event.set()
        watchdog.cancel()
        db_now: Db | None = app.state.db
        if db_now is not None:
            await db_now.close()


def create_app(env: dict[str, str] | None = None) -> FastAPI:
    """Фабрика приложения. env — переопределение окружения (тесты/локальный запуск)."""
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
    app = FastAPI(
        title="ПроСобеседование API",
        version="2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if env is not None:
        app.state.env_override = env
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(webhook.router)
    return app


def main() -> None:
    import uvicorn

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    _configure_logging(log_level)
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8080"))
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,  # идентификаторы в технических логах псевдонимизированы (§3.9)
    )


if __name__ == "__main__":
    main()
