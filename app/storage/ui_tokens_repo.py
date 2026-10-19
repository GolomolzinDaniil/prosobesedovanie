"""Репозиторий ui_tokens (§3.1): одноразовые непрозрачные callback-токены.

Храним действие, разрешённое значение, владельца, версию и срок.
Срок выбора параметров 30 минут, подтверждения удаления и отмены 5 минут,
управления текущим вопросом 24 часа. Одноразовый токен атомарно потребляется
(UPDATE ... WHERE consumed_at IS NULL). После устаревания показывается
актуальный экран.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import asyncpg

TOKEN_COLUMNS = (
    "token, user_id, action, allowed_value, session_id, expected_version, "
    "expires_at, consumed_at, created_at"
)

SELECTION_ACTIONS = (
    "select_role",
    "select_level",
    "select_format",
    "back_to_role",
    "back_to_level",
    "back_to_format",
    "start_session",
    "consent_accept",
    "consent_decline",
)


async def issue(
    conn: asyncpg.Connection,
    *,
    token: str,
    user_id: uuid.UUID,
    action: str,
    ttl: timedelta,
    allowed_value: str | None = None,
    session_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO ui_tokens (token, user_id, action, allowed_value, session_id,
                               expected_version, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, now() + $7::interval)
        """,
        token,
        user_id,
        action,
        allowed_value,
        session_id,
        expected_version,
        ttl,
    )


async def consume(
    conn: asyncpg.Connection, token: str, *, telegram_user_id: int
) -> asyncpg.Record | None:
    """Атомарное одноразовое потребление с проверкой владельца и срока.

    None — токен неизвестен, уже использован, чужой или истёк (вызывающий
    код показывает STALE_ACTION и актуальный экран, §3.1).
    """
    return await conn.fetchrow(
        """
        UPDATE ui_tokens AS t
        SET consumed_at = now()
        WHERE t.token = $1
          AND t.consumed_at IS NULL
          AND t.expires_at > now()
          AND EXISTS (
              SELECT 1 FROM users u
              WHERE u.id = t.user_id AND u.telegram_user_id = $2
          )
        RETURNING t.token, t.user_id, t.action, t.allowed_value, t.session_id,
                  t.expected_version, t.expires_at, t.consumed_at, t.created_at
        """,
        token,
        telegram_user_id,
    )


async def revoke_unconsumed(
    conn: asyncpg.Connection, user_id: uuid.UUID, actions: tuple[str, ...] = SELECTION_ACTIONS
) -> int:
    """Делает прежние кнопки экрана недействительными при перерисовке
    (старые кнопки не управляют новым состоянием, §2.4)."""
    result = await conn.execute(
        """
        UPDATE ui_tokens
        SET consumed_at = now()
        WHERE user_id = $1 AND consumed_at IS NULL AND action = ANY($2::text[])
        """,
        user_id,
        list(actions),
    )
    return int(result.split()[-1])


async def find(conn: asyncpg.Connection, token: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {TOKEN_COLUMNS} FROM ui_tokens WHERE token = $1", token
    )
