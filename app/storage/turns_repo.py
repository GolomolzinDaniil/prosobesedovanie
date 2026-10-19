"""Репозиторий turns (§2.3): вопросы, ответы, пропуски, черновики расшифровок.

Ответ и assess-задание атомарны (§2.2): сохранение выполняется транзакцией
вместе с созданием задания и подтверждением в outbox. Двойной ответ одному
turn_id после первого принятия возвращает BUSY и не перезаписывает ответ (§2.4).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

TURN_COLUMNS = (
    "id, session_id, ordinal, kind, question_type, main_index, parent_turn_id, topic, "
    "question_text, rubric, difficulty, catalog_entry_id, answer_text, answer_received_at, "
    "skipped, assessment, assessed_at, transcript_version, draft_transcript, stt_attempts, "
    "delivered_message_id, created_at"
)


async def create_main_turn(
    conn: asyncpg.Connection,
    *,
    session_id: uuid.UUID,
    ordinal: int,
    main_index: int,
    question_type: str,
    topic: str,
    question_text: str,
    rubric: dict[str, Any],
    catalog_entry_id: str,
    difficulty: int | None,
    turn_id: uuid.UUID | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO turns (id, session_id, ordinal, kind, question_type, main_index,
                           topic, question_text, rubric, difficulty, catalog_entry_id)
        VALUES (COALESCE($1, gen_random_uuid()), $2, $3, 'main', $4, $5, $6, $7,
                $8::jsonb, $9, $10)
        RETURNING {TURN_COLUMNS}
        """,
        turn_id,
        session_id,
        ordinal,
        question_type,
        main_index,
        topic,
        question_text,
        json.dumps(rubric, ensure_ascii=False),
        difficulty,
        catalog_entry_id,
    )


async def create_follow_up_turn(
    conn: asyncpg.Connection,
    *,
    session_id: uuid.UUID,
    ordinal: int,
    parent_turn_id: uuid.UUID,
    question_type: str,
    topic: str,
    question_text: str,
    rubric: dict[str, Any],
    catalog_entry_id: str,
    difficulty: int | None,
    turn_id: uuid.UUID | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO turns (id, session_id, ordinal, kind, question_type, parent_turn_id,
                           topic, question_text, rubric, difficulty, catalog_entry_id)
        VALUES (COALESCE($1, gen_random_uuid()), $2, $3, 'follow_up', $4, $5, $6, $7,
                $8::jsonb, $9, $10)
        RETURNING {TURN_COLUMNS}
        """,
        turn_id,
        session_id,
        ordinal,
        question_type,
        parent_turn_id,
        topic,
        question_text,
        json.dumps(rubric, ensure_ascii=False),
        difficulty,
        catalog_entry_id,
    )


async def find_by_id(
    conn: asyncpg.Connection, turn_id: uuid.UUID, *, for_update: bool = False
) -> asyncpg.Record | None:
    lock = " FOR UPDATE" if for_update else ""
    return await conn.fetchrow(
        f"SELECT {TURN_COLUMNS} FROM turns WHERE id = $1{lock}", turn_id
    )


async def list_by_session(conn: asyncpg.Connection, session_id: uuid.UUID) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {TURN_COLUMNS} FROM turns WHERE session_id = $1 ORDER BY ordinal", session_id
    )


async def current_main_index(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    """Максимальный main_index среди основных вопросов сессии (0 если нет)."""
    value = await conn.fetchval(
        "SELECT max(main_index)::int FROM turns WHERE session_id = $1 AND kind = 'main'",
        session_id,
    )
    return value or 0


async def next_ordinal(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    value = await conn.fetchval(
        "SELECT max(ordinal)::int FROM turns WHERE session_id = $1", session_id
    )
    return (value or 0) + 1


async def save_answer(
    conn: asyncpg.Connection, turn_id: uuid.UUID, normalized_text: str
) -> bool:
    """Сохраняет ответ только если он ещё не принят (защита от двойного
    принятия на уровне строки). False — ответ уже принят или turn пропущен."""
    row = await conn.fetchrow(
        """
        UPDATE turns
        SET answer_text = $2, answer_received_at = now()
        WHERE id = $1 AND answer_text IS NULL AND skipped = FALSE
        RETURNING id
        """,
        turn_id,
        normalized_text,
    )
    return row is not None


async def mark_skipped(conn: asyncpg.Connection, turn_id: uuid.UUID) -> bool:
    """Пропуск: answer_text=NULL, оценка отсутствует (§3.3)."""
    row = await conn.fetchrow(
        """
        UPDATE turns
        SET skipped = TRUE
        WHERE id = $1 AND answer_text IS NULL AND skipped = FALSE
        RETURNING id
        """,
        turn_id,
    )
    return row is not None


async def set_delivered_message_id(
    conn: asyncpg.Connection, turn_id: uuid.UUID, message_id: int
) -> None:
    await conn.execute(
        "UPDATE turns SET delivered_message_id = $2 WHERE id = $1", turn_id, message_id
    )


async def count_answered(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*)::int FROM turns WHERE session_id = $1 AND answer_text IS NOT NULL",
        session_id,
    )


async def count_skipped(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*)::int FROM turns WHERE session_id = $1 AND skipped", session_id
    )


async def has_follow_up(conn: asyncpg.Connection, parent_turn_id: uuid.UUID) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM turns WHERE parent_turn_id = $1 LIMIT 1", parent_turn_id
        )
    )
