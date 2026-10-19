"""Репозиторий jobs (§2.1–2.2): идемпотентное создание заданий, отмена.

Ответ и assess-задание создаются в одной транзакции (§2.2). unique_key
гарантирует, что повторная обработка update не создаёт второе задание.
Обработчики AI-заданий (assess/transcribe/report/tts) подключаются в частях
3–4; до этого задания остаются pending — сохранённый ответ не теряется.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

JOB_COLUMNS = (
    "id, unique_key, session_id, turn_id, kind, api_version, state, attempts, "
    "expected_version, lease_owner, lease_expires_at, payload, result, "
    "media_storage_key, error_code, created_at, started_at, finished_at"
)


async def create(
    conn: asyncpg.Connection,
    *,
    unique_key: str,
    session_id: uuid.UUID,
    kind: str,
    expected_version: int,
    payload: dict[str, Any],
    turn_id: uuid.UUID | None = None,
) -> asyncpg.Record | None:
    """Создаёт задание; None если unique_key уже существует (идемпотентность)."""
    return await conn.fetchrow(
        f"""
        INSERT INTO jobs (unique_key, session_id, turn_id, kind, expected_version, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (unique_key) DO NOTHING
        RETURNING {JOB_COLUMNS}
        """,
        unique_key,
        session_id,
        turn_id,
        kind,
        expected_version,
        json.dumps(payload, ensure_ascii=False),
    )


async def cancel_by_session(conn: asyncpg.Connection, session_id: uuid.UUID) -> int:
    """Отмена заданий сессии (§2.2 удаление/отмена): pending и leased ->
    cancelled; исполнители проверяют состояние при commit (поздний результат
    устаревшей версии отбрасывается, §2.4)."""
    result = await conn.execute(
        """
        UPDATE jobs
        SET state = 'cancelled', finished_at = now()
        WHERE session_id = $1 AND state IN ('pending', 'leased')
        """,
        session_id,
    )
    return int(result.split()[-1])


async def count_by_session_state(
    conn: asyncpg.Connection, session_id: uuid.UUID, states: tuple[str, ...]
) -> int:
    return await conn.fetchval(
        "SELECT count(*)::int FROM jobs WHERE session_id = $1 AND state = ANY($2::text[])",
        session_id,
        list(states),
    )


# ---------------------------------------------------------------------------
# Worker-механика заданий (§2.1, §3.2): lease, SKIP LOCKED, результат
# ---------------------------------------------------------------------------


async def claim_next(
    conn: asyncpg.Connection,
    *,
    kinds: tuple[str, ...],
    owner: str,
    lease_seconds: int,
) -> asyncpg.Record | None:
    """Забирает ближайшее задание под lease (номер попытки увеличивается).

    Просроченные lease возвращаются в очередь отдельным вызовом
    recover_expired_leases — так lease-expiry учитывается в метриках (§3.10)."""
    return await conn.fetchrow(
        """
        UPDATE jobs AS j
        SET state = 'leased',
            lease_owner = $2,
            lease_expires_at = now() + make_interval(secs => $3),
            attempts = j.attempts + 1,
            started_at = COALESCE(j.started_at, now())
        WHERE j.id = (
            SELECT c.id FROM jobs c
            WHERE c.state = 'pending' AND c.kind = ANY($1::text[])
            ORDER BY c.created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING j.*
        """,
        list(kinds),
        owner,
        float(lease_seconds),
    )


async def recover_expired_leases(conn: asyncpg.Connection) -> int:
    """Просроченный lease возвращается в очередь (§3.2); счёт — для метрик."""
    rows = await conn.execute(
        """
        UPDATE jobs
        SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL
        WHERE state = 'leased' AND lease_expires_at < now()
        """
    )
    parts = rows.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


async def renew_lease(
    conn: asyncpg.Connection, job_id: uuid.UUID, *, owner: str, lease_seconds: int
) -> bool:
    """Heartbeat-продление lease (каждые worker_heartbeat секунд, §3.2)."""
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET lease_expires_at = now() + make_interval(secs => $3)
        WHERE id = $1 AND lease_owner = $2 AND state = 'leased'
        RETURNING id
        """,
        job_id,
        owner,
        float(lease_seconds),
    )
    return row is not None


async def lock_for_commit(
    conn: asyncpg.Connection, job_id: uuid.UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = $1 FOR UPDATE", job_id
    )


async def mark_done(
    conn: asyncpg.Connection,
    job_id: uuid.UUID,
    *,
    owner: str,
    result: dict[str, Any],
) -> bool:
    """Фиксация результата: два worker не могут применить один результат —
    lease_owner и состояние проверяются при commit (§3.2)."""
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET state = 'done', result = $3::jsonb, finished_at = now(),
            lease_owner = NULL, lease_expires_at = NULL
        WHERE id = $1 AND lease_owner = $2 AND state = 'leased'
        RETURNING id
        """,
        job_id,
        owner,
        json.dumps(result, ensure_ascii=False),
    )
    return row is not None


async def mark_failed(
    conn: asyncpg.Connection,
    job_id: uuid.UUID,
    *,
    owner: str,
    error_code: str,
    result: dict[str, Any] | None = None,
) -> bool:
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET state = 'failed', error_code = $3, finished_at = now(),
            result = COALESCE($4::jsonb, result),
            lease_owner = NULL, lease_expires_at = NULL
        WHERE id = $1 AND lease_owner = $2 AND state = 'leased'
        RETURNING id
        """,
        job_id,
        owner,
        error_code,
        json.dumps(result, ensure_ascii=False) if result is not None else None,
    )
    return row is not None


async def sync_expected_version(
    conn: asyncpg.Connection, session_id: uuid.UUID, new_version: int
) -> int:
    """Синхронизирует ожидаемую версию открытых заданий после управляющего
    изменения, которое НЕ инвалидирует текущую оценку (requested_action=finish,
    §2.4): оценка применяется, новая тема не создаётся."""
    result = await conn.execute(
        """
        UPDATE jobs SET expected_version = $2
        WHERE session_id = $1 AND state IN ('pending', 'leased') AND kind = 'assess'
        """,
        session_id,
        new_version,
    )
    return int(result.split()[-1])


async def count_provider_calls(conn: asyncpg.Connection, job_id: uuid.UUID) -> int:
    """Фактическое число вызовов провайдера для операции (§3.2: не более 2)."""
    return await conn.fetchval(
        "SELECT count(*)::int FROM ai_calls WHERE job_id = $1", job_id
    )
