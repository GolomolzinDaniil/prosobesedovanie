"""Доступ к PostgreSQL (§2.1): пул asyncpg и проверка доступности.

Между процессами API и worker используется PostgreSQL; вызовы доменных
методов внутри процесса обычные, не сетевые. Доступ к БД через закрытую
сеть; в production разные роли для приложения, миграций и backup (§3.9).
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)


class Db:
    """Обёртка пула соединений приложения."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 5.0,
    ) -> Db:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            command_timeout=15.0,
        )
        return cls(pool)

    async def ping(self) -> bool:
        """SELECT 1 — для /health/ready и webhook-деградации (503)."""
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            logger.warning("db ping failed: %s", type(exc).__name__)
            return False

    async def close(self) -> None:
        await self.pool.close()


async def try_connect(dsn: str, *, min_size: int = 1, max_size: int = 10) -> Db | None:
    """Попытка подключения без падения процесса: API обязан стартовать и
    возвращать 503, пока PostgreSQL недоступен (§3.1)."""
    try:
        return await Db.create(dsn, min_size=min_size, max_size=max_size)
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        logger.warning("db unavailable at startup: %s", type(exc).__name__)
        return None
