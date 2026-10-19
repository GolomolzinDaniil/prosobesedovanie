"""Репозиторий users (§2.3): профиль, согласие, UI-состояние, удаление.

Порядок блокировок постоянный: users -> sessions -> credits -> связанные
строки (§2.3). Все доменные транзакции начинаются с блокировки строки user.
"""

from __future__ import annotations

import uuid

import asyncpg

USER_COLUMNS = (
    "id, telegram_user_id, chat_id, ui_state, version, consent_version, consent_at, "
    "source_code, deletion_requested_at, created_at, updated_at"
)


async def find_by_telegram_id(
    conn: asyncpg.Connection, telegram_user_id: int
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {USER_COLUMNS} FROM users WHERE telegram_user_id = $1", telegram_user_id
    )


async def lock_by_telegram_id(
    conn: asyncpg.Connection, telegram_user_id: int
) -> asyncpg.Record | None:
    """SELECT ... FOR UPDATE — начало доменной транзакции."""
    return await conn.fetchrow(
        f"SELECT {USER_COLUMNS} FROM users WHERE telegram_user_id = $1 FOR UPDATE",
        telegram_user_id,
    )


async def lock_by_id(conn: asyncpg.Connection, user_id: uuid.UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {USER_COLUMNS} FROM users WHERE id = $1 FOR UPDATE", user_id
    )


async def create(
    conn: asyncpg.Connection,
    *,
    telegram_user_id: int,
    chat_id: int,
    ui_state: str = "awaiting_consent",
    source_code: str | None = None,
) -> asyncpg.Record:
    """Создаёт профиль. Кампания source_code — только из allowlist (§3.7)."""
    return await conn.fetchrow(
        f"""
        INSERT INTO users (telegram_user_id, chat_id, ui_state, source_code)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_user_id) DO UPDATE SET updated_at = now()
        RETURNING {USER_COLUMNS}
        """,
        telegram_user_id,
        chat_id,
        ui_state,
        source_code,
    )


async def set_consent(
    conn: asyncpg.Connection, user_id: uuid.UUID, consent_version: str
) -> None:
    """Кнопка согласия записывает версию документа и дату (§3.7)."""
    await conn.execute(
        """
        UPDATE users
        SET consent_version = $2, consent_at = now(), version = version + 1,
            updated_at = now()
        WHERE id = $1 AND consent_version IS NULL
        """,
        user_id,
        consent_version,
    )


async def set_ui_state(conn: asyncpg.Connection, user_id: uuid.UUID, ui_state: str) -> None:
    await conn.execute(
        "UPDATE users SET ui_state = $2, version = version + 1, updated_at = now() WHERE id = $1",
        user_id,
        ui_state,
    )


async def request_deletion(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    """Признак deletion_requested_at: новые ответы и покупки не принимаются
    до завершения операции (§2.2, §3.7)."""
    await conn.execute(
        """
        UPDATE users
        SET deletion_requested_at = now(), ui_state = 'deletion_pending',
            version = version + 1, updated_at = now()
        WHERE id = $1 AND deletion_requested_at IS NULL
        """,
        user_id,
    )
