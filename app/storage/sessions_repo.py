"""Репозиторий sessions (§2.3–2.4): статусы, версии, одна активная на пользователя.

Версия увеличивается при каждом изменении управления сессией (§2.4).
Неактивная сессия истекает через 24 часа после последнего принятого действия
пользователя; фоновые повторы срок не продлевают (touch — только из
пользовательских транзакций).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from contracts.models import TERMINAL_STATUSES, SessionStatus

ACTIVE_FILTER = "status NOT IN ('completed', 'cancelled', 'expired', 'failed')"

SESSION_COLUMNS = (
    "id, user_id, role_id, level, format, status, version, requested_action, "
    "catalog_version, prompt_version, model_profile_version, current_turn_id, "
    "current_difficulty, follow_ups_used, last_user_action_at, expires_at, "
    "created_at, finished_at"
)


async def find_active(
    conn: asyncpg.Connection, user_id: uuid.UUID, *, for_update: bool = False
) -> asyncpg.Record | None:
    lock = " FOR UPDATE" if for_update else ""
    return await conn.fetchrow(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE user_id = $1 AND {ACTIVE_FILTER}{lock}",
        user_id,
    )


async def find_by_id(
    conn: asyncpg.Connection, session_id: uuid.UUID, *, for_update: bool = False
) -> asyncpg.Record | None:
    lock = " FOR UPDATE" if for_update else ""
    return await conn.fetchrow(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = $1{lock}", session_id
    )


async def count_starts_since(
    conn: asyncpg.Connection, user_id: uuid.UUID, since: datetime
) -> int:
    """Старты за скользящие 24 часа; отменённый и неудачный старт учитываются
    в лимите (§1.3)."""
    return await conn.fetchval(
        "SELECT count(*)::int FROM sessions WHERE user_id = $1 AND created_at > $2",
        user_id,
        since,
    )


async def oldest_start_since(
    conn: asyncpg.Connection, user_id: uuid.UUID, since: datetime
) -> datetime | None:
    return await conn.fetchval(
        "SELECT min(created_at) FROM sessions WHERE user_id = $1 AND created_at > $2",
        user_id,
        since,
    )


async def create(
    conn: asyncpg.Connection,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: str,
    level: str,
    fmt: str,
    catalog_version: str,
    prompt_version: str,
    model_profile_version: str | None,
    current_difficulty: int,
    inactive_ttl: timedelta,
) -> asyncpg.Record:
    now = datetime.now(UTC)
    return await conn.fetchrow(
        f"""
        INSERT INTO sessions (id, user_id, role_id, level, format, status, version,
                              catalog_version, prompt_version, model_profile_version,
                              current_difficulty, last_user_action_at, expires_at, created_at)
        VALUES ($1, $2, $3, $4, $5, 'preparing', 1, $6, $7, $8, $9, $10, $11, $10)
        RETURNING {SESSION_COLUMNS}
        """,
        session_id,
        user_id,
        role_id,
        level,
        fmt,
        catalog_version,
        prompt_version,
        model_profile_version,
        current_difficulty,
        now,
        now + inactive_ttl,
    )


async def transition(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    status: SessionStatus,
    *,
    expected_statuses: tuple[SessionStatus, ...] | None = None,
    requested_action: str | None = None,
    touch_user_action: bool = False,
    inactive_ttl: timedelta | None = None,
) -> asyncpg.Record | None:
    """Переход статуса с optimistic-guard по текущему статусу и bump версии.

    Возвращает обновлённую строку либо None, если guard не совпал (поздний/
    конкурентный вызов — домен интерпретирует как BUSY/STALE, §2.4).
    """
    terminal = status in TERMINAL_STATUSES
    sets = [
        "status = $2",
        "version = version + 1",
        "finished_at = CASE WHEN $3::boolean THEN now() ELSE finished_at END",
    ]
    params: list = [session_id, status.value, terminal]
    if requested_action is not None:
        params.append(requested_action)
        sets.append(f"requested_action = ${len(params)}::text")
    if touch_user_action:
        ttl = inactive_ttl or timedelta(hours=24)
        params.append(ttl)
        sets.append("last_user_action_at = now()")
        sets.append(f"expires_at = now() + ${len(params)}::interval")
    sql = f"UPDATE sessions SET {', '.join(sets)} WHERE id = $1"
    if expected_statuses is not None:
        placeholders = ", ".join(
            f"${len(params) + 1 + i}" for i in range(len(expected_statuses))
        )
        params.extend(s.value for s in expected_statuses)
        sql += f" AND status IN ({placeholders})"
    sql += f" RETURNING {SESSION_COLUMNS}"
    return await conn.fetchrow(sql, *params)


async def set_current_turn(
    conn: asyncpg.Connection, session_id: uuid.UUID, turn_id: uuid.UUID | None
) -> None:
    await conn.execute(
        "UPDATE sessions SET current_turn_id = $2 WHERE id = $1", session_id, turn_id
    )


async def set_requested_action(
    conn: asyncpg.Connection, session_id: uuid.UUID, action: str
) -> int:
    """Фиксирует запрошенное действие; возвращает новую версию сессии."""
    return await conn.fetchval(
        "UPDATE sessions SET requested_action = $2, version = version + 1 "
        "WHERE id = $1 RETURNING version",
        session_id,
        action,
    )


async def set_difficulty(
    conn: asyncpg.Connection, session_id: uuid.UUID, difficulty: int
) -> None:
    await conn.execute(
        "UPDATE sessions SET current_difficulty = $2 WHERE id = $1", session_id, difficulty
    )
