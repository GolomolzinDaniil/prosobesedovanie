"""Репозиторий credits (§3.8): пробная попытка, резерв, освобождение.

Первое собеседование бесплатно: одна trial-credit при первом согласованном
входе, UNIQUE(user_id) для source=trial. При старте доступный credit
выбирается по порядку trial -> grant -> payment и резервируется; резерв
предотвращает параллельное использование. Отмена/ошибка возвращают available;
consumed — только после доставленного отчёта (часть 4).
"""

from __future__ import annotations

import uuid

import asyncpg

_SOURCE_PRIORITY = "CASE source WHEN 'trial' THEN 0 WHEN 'grant' THEN 1 ELSE 2 END"


async def ensure_trial_credit(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    """Идемпотентно создаёт одну пробную попытку (частичный UNIQUE-индекс)."""
    await conn.execute(
        """
        INSERT INTO credits (user_id, source, state)
        VALUES ($1, 'trial', 'available')
        ON CONFLICT (user_id) WHERE source = 'trial' DO NOTHING
        """,
        user_id,
    )


async def reserve_available(
    conn: asyncpg.Connection, user_id: uuid.UUID, session_id: uuid.UUID
) -> asyncpg.Record | None:
    """Резервирует одну доступную попытку (trial -> grant -> payment).

    Вызывается внутри транзакции старта после блокировки user и проверки
    отсутствия активной сессии; BEGIN/COMMIT атомарны с созданием сессии (§2.3).
    """
    return await conn.fetchrow(
        f"""
        UPDATE credits
        SET state = 'reserved', reserved_session_id = $2, updated_at = now()
        WHERE id = (
            SELECT id FROM credits
            WHERE user_id = $1 AND state = 'available'
            ORDER BY {_SOURCE_PRIORITY}, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        user_id,
        session_id,
    )


async def release_reservation(conn: asyncpg.Connection, credit_id: uuid.UUID) -> None:
    """Отмена, истечение без отчёта, отсутствие ответов и окончательная
    техническая ошибка возвращают available (§3.8)."""
    await conn.execute(
        """
        UPDATE credits
        SET state = 'available', reserved_session_id = NULL, updated_at = now()
        WHERE id = $1 AND state = 'reserved'
        """,
        credit_id,
    )


async def release_by_session(conn: asyncpg.Connection, session_id: uuid.UUID) -> None:
    await conn.execute(
        """
        UPDATE credits
        SET state = 'available', reserved_session_id = NULL, updated_at = now()
        WHERE reserved_session_id = $1 AND state = 'reserved'
        """,
        session_id,
    )


async def count_by_state(conn: asyncpg.Connection, user_id: uuid.UUID, state: str) -> int:
    return await conn.fetchval(
        "SELECT count(*)::int FROM credits WHERE user_id = $1 AND state = $2",
        user_id,
        state,
    )


async def trial_used(conn: asyncpg.Connection, user_id: uuid.UUID) -> bool:
    row = await conn.fetchrow(
        "SELECT state FROM credits WHERE user_id = $1 AND source = 'trial'", user_id
    )
    return row is not None and row["state"] in ("reserved", "consumed", "revoked")


async def find_reserved_by_session(
    conn: asyncpg.Connection, session_id: uuid.UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM credits WHERE reserved_session_id = $1 AND state = 'reserved' "
        "FOR UPDATE",
        session_id,
    )
