"""Репозиторий events (§3.10): бизнес-события с дедупликацией по переходу.

Дедупликация события по бизнес-переходу (dedup_key), не только по update_id.
Ответы в событиях не хранятся; свойства — только числовые/категориальные.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg


async def emit(
    conn: asyncpg.Connection,
    *,
    dedup_key: str,
    name: str,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    props_numeric: dict[str, Any] | None = None,
    props_category: dict[str, Any] | None = None,
) -> bool:
    """Создаёт событие; False если dedup_key уже зарегистрирован."""
    row = await conn.fetchrow(
        """
        INSERT INTO events (dedup_key, name, user_id, session_id,
                            props_numeric, props_category)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        ON CONFLICT (dedup_key) DO NOTHING
        RETURNING id
        """,
        dedup_key,
        name,
        user_id,
        session_id,
        json.dumps(props_numeric or {}, ensure_ascii=False),
        json.dumps(props_category or {}, ensure_ascii=False),
    )
    return row is not None
