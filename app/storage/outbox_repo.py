"""Репозиторий outbox (§2.2, §2.4, §3.2): надёжная очередь исходящих сообщений.

Порядок: для question_text и report_part используется порядок outbox —
следующий обязательный текст не отправляется, пока предыдущий не подтверждён.
Необязательные строки (озвучка вопроса) не задерживают обязательные.

Повторы: до 10 в пределах 24 часов с backoff; при 429 соблюдается retry_after.
Telegram 403 (чат недоступен) и 400 (некорректный payload) не повторяются
бесконечно. При восстановлении не отправляется результат отменённой/удалённой
сессии — sender повторно проверяет признак удаления перед отправкой.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

OUTBOX_COLUMNS = (
    "id, unique_key, user_id, session_id, chat_id, kind, seq, is_required, state, "
    "payload, reply_to_message_id, reply_to_outbox_id, message_id, attempts, receipts, "
    "next_attempt_at, last_error, created_at, delivered_at"
)

# Дедлайн попытки отправки: зависшая 'sending' возвращается в pending
SEND_DEADLINE_SECONDS = 60


async def enqueue(
    conn: asyncpg.Connection,
    *,
    unique_key: str,
    user_id: uuid.UUID,
    chat_id: int,
    kind: str,
    payload: dict[str, Any],
    session_id: uuid.UUID | None = None,
    is_required: bool = True,
    reply_to_message_id: int | None = None,
    reply_to_outbox_id: uuid.UUID | None = None,
) -> asyncpg.Record | None:
    """Идемпотентное создание строки: повтор unique_key не дублирует сообщение."""
    seq = await conn.fetchval(
        "SELECT COALESCE(max(seq), 0) + 1 FROM outbox WHERE chat_id = $1", chat_id
    )
    return await conn.fetchrow(
        f"""
        INSERT INTO outbox (unique_key, user_id, session_id, chat_id, kind, seq,
                            is_required, state, payload, reply_to_message_id,
                            reply_to_outbox_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8::jsonb, $9, $10)
        ON CONFLICT (unique_key) DO NOTHING
        RETURNING {OUTBOX_COLUMNS}
        """,
        unique_key,
        user_id,
        session_id,
        chat_id,
        kind,
        seq,
        is_required,
        json.dumps(payload, ensure_ascii=False),
        reply_to_message_id,
        reply_to_outbox_id,
    )


async def claim_next(
    conn: asyncpg.Connection, *, now: datetime | None = None
) -> asyncpg.Record | None:
    """Ближайшая готовая к отправке строка с учётом порядка на чат.

    Порядок outbox применяется к question_text и report_part (§2.4): следующий
    обязательный текст этого потока не отправляется, пока предыдущий не
    подтверждён. Прочие виды (подтверждения, кнопки, озвучка) обязательный
    поток не блокируют. Зависшие 'sending' с истёкшим дедлайном возвращаются
    в очередь.
    """
    moment = now or datetime.now(UTC)
    return await conn.fetchrow(
        """
        WITH recovered AS (
            -- зависшая 'sending' (сбой процесса) возвращается в очередь;
            -- эффект CTE виден со следующего опроса (единый снимок оператора)
            UPDATE outbox
            SET state = 'pending'
            WHERE state = 'sending' AND next_attempt_at < $1::timestamptz
            RETURNING id
        )
        UPDATE outbox AS o
        SET state = 'sending',
            attempts = o.attempts + 1,
            next_attempt_at = $1::timestamptz + make_interval(secs => $2)
        WHERE o.id = (
            SELECT c.id
            FROM outbox c
            WHERE c.state = 'pending'
              AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= $1::timestamptz)
              AND (
                  c.kind NOT IN ('question_text', 'report_part')
                  OR NOT EXISTS (
                      SELECT 1 FROM outbox p
                      WHERE p.chat_id = c.chat_id
                        AND p.seq < c.seq
                        AND p.is_required
                        AND p.kind IN ('question_text', 'report_part')
                        AND p.state IN ('pending', 'sending')
                  )
              )
            ORDER BY c.created_at, c.seq
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING o.*
        """,
        moment,
        float(SEND_DEADLINE_SECONDS),
    )


async def mark_delivered(
    conn: asyncpg.Connection,
    outbox_id: uuid.UUID,
    *,
    message_id: int,
    receipts_extra: dict[str, Any] | None = None,
) -> None:
    """Подтверждение Bot API: сохраняем message_id; receipts содержат только
    известные подтверждения (§2.3)."""
    receipts: dict[str, Any] = {"message_ids": [message_id]}
    if receipts_extra:
        receipts.update(receipts_extra)
    await conn.execute(
        """
        UPDATE outbox
        SET state = 'delivered', message_id = $2, delivered_at = now(),
            next_attempt_at = NULL, last_error = NULL,
            receipts = receipts || $3::jsonb
        WHERE id = $1
        """,
        outbox_id,
        message_id,
        json.dumps(receipts, ensure_ascii=False),
    )


async def schedule_retry(
    conn: asyncpg.Connection,
    outbox_id: uuid.UUID,
    *,
    next_attempt_at: datetime,
    error: str,
) -> None:
    await conn.execute(
        """
        UPDATE outbox
        SET state = 'pending', next_attempt_at = $2, last_error = left($3, 500)
        WHERE id = $1
        """,
        outbox_id,
        next_attempt_at,
        error,
    )


async def mark_failed(
    conn: asyncpg.Connection, outbox_id: uuid.UUID, *, error: str
) -> asyncpg.Record:
    """Терминальный отказ доставки (исчерпаны повторы либо 403/400)."""
    row = await conn.fetchrow(
        f"""
        UPDATE outbox
        SET state = 'failed', last_error = left($2, 500), next_attempt_at = NULL
        WHERE id = $1
        RETURNING {OUTBOX_COLUMNS}
        """,
        outbox_id,
        error,
    )
    return row


async def get_by_id(conn: asyncpg.Connection, outbox_id: uuid.UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {OUTBOX_COLUMNS} FROM outbox WHERE id = $1", outbox_id
    )


async def get_by_unique_key(
    conn: asyncpg.Connection, unique_key: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {OUTBOX_COLUMNS} FROM outbox WHERE unique_key = $1", unique_key
    )


async def known_message_ids(
    conn: asyncpg.Connection, unique_keys: list[str]
) -> set[int]:
    """Известные подтверждения доставок (копии сообщения) для проверки reply."""
    if not unique_keys:
        return set()
    rows = await conn.fetch(
        """
        SELECT message_id, receipts FROM outbox
        WHERE unique_key = ANY($1::text[]) AND state = 'delivered'
        """,
        unique_keys,
    )
    ids: set[int] = set()
    for row in rows:
        if row["message_id"] is not None:
            ids.add(row["message_id"])
        receipts = row["receipts"] or {}
        if isinstance(receipts, str):
            receipts = json.loads(receipts)
        for mid in receipts.get("message_ids", []):
            ids.add(int(mid))
    return ids


async def cancel_pending_by_session(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    result = await conn.execute(
        """
        UPDATE outbox SET state = 'cancelled'
        WHERE session_id = $1 AND state IN ('pending', 'sending')
        """,
        session_id,
    )
    return int(result.split()[-1])


async def cancel_pending_by_user(conn: asyncpg.Connection, user_id: uuid.UUID) -> int:
    result = await conn.execute(
        """
        UPDATE outbox SET state = 'cancelled'
        WHERE user_id = $1 AND state IN ('pending', 'sending')
        """,
        user_id,
    )
    return int(result.split()[-1])


async def retry_deadline_exceeded(
    row: asyncpg.Record, *, window: timedelta, max_retries: int
) -> bool:
    """Повторы исчерпаны: attempts >= max_retries либо окно 24 ч закрыто."""
    if row["attempts"] >= max_retries:
        return True
    created = row["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > window
