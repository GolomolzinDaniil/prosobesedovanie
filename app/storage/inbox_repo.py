"""Репозиторий inbox: сохранение обновлений с дедупликацией и надёжная выборка.

Дедупликация (§2.2, §3.1): update_id — PRIMARY KEY; повтор update_id
подтверждается HTTP 200 без повторного выполнения команды (ON CONFLICT DO
NOTHING, доменный эффект не повторяется).

Выборка заданий (§2.1): SELECT ... FOR UPDATE SKIP LOCKED + lease.
Просроченный lease возвращается в очередь; номер попытки увеличивается;
обработчик повторяется до max_retries раз (§3.2), после чего строка
переводится в failed (эффекты вынесены в outbox/jobs и безопасно повторяются).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg


@dataclass(frozen=True)
class InboxItem:
    update_id: int
    kind: str
    status: str
    attempts: int
    payload: dict[str, Any] | None
    user_id: str | None
    received_at: datetime


async def insert_update(
    conn: asyncpg.Connection,
    *,
    update_id: int,
    kind: str,
    payload: dict[str, Any],
    initial_status: str = "pending",
) -> bool:
    """Сохраняет обновление; False если update_id уже известен (дубликат).

    initial_status='skipped' — для неподдерживаемых видов: обновление признано
    обработанным без вызова AI (§3.1), processed_at фиксируется сразу.
    """
    if initial_status not in ("pending", "skipped"):
        raise ValueError("initial_status обязан быть pending или skipped")
    row = await conn.fetchrow(
        """
        INSERT INTO inbox (update_id, kind, status, payload, processed_at)
        VALUES ($1, $2, $3, $4::jsonb,
                CASE WHEN $3 = 'skipped' THEN now() ELSE NULL END)
        ON CONFLICT (update_id) DO NOTHING
        RETURNING update_id
        """,
        update_id,
        kind,
        initial_status,
        _dumps(payload),
    )
    return row is not None


async def get_update(conn: asyncpg.Connection, update_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM inbox WHERE update_id = $1", update_id)


async def attach_user(conn: asyncpg.Connection, update_id: int, user_id) -> None:
    """Связывает обновление с профилем после разрешения пользователя
    (нужно для лимита команд и учёта, §1.3)."""
    await conn.execute(
        "UPDATE inbox SET user_id = $2 WHERE update_id = $1 AND user_id IS DISTINCT FROM $2",
        update_id,
        user_id,
    )


async def claim_batch(
    conn: asyncpg.Connection,
    *,
    owner: str,
    limit: int = 10,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> list[asyncpg.Record]:
    """Забирает пакет обновлений под lease: competing consumers не пересекаются.

    Кандидаты: status='pending' с наступившим next_attempt_at, либо
    status='processing' с просроченным lease (перезапуск worker, §3.2).
    """
    moment = now or datetime.now(UTC)
    return await conn.fetch(
        """
        UPDATE inbox AS i
        SET status = 'processing',
            lease_owner = $1,
            lease_expires_at = $2::timestamptz + make_interval(secs => $3),
            attempts = i.attempts + 1
        WHERE i.update_id IN (
            SELECT update_id
            FROM inbox
            WHERE (status = 'pending'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= $2::timestamptz))
               OR (status = 'processing' AND lease_expires_at < $2::timestamptz)
            ORDER BY received_at
            LIMIT $4
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        owner,
        moment,
        float(lease_seconds),
        limit,
    )


async def mark_processed(
    conn: asyncpg.Connection,
    update_id: int,
    *,
    purge_payload: bool = False,
) -> None:
    """Терминальный успех обработки. payload обнуляется политикой хранения
    (§3.9) — здесь по желанию вызывающего (обычно это делает retention-sweep)."""
    await conn.execute(
        """
        UPDATE inbox
        SET status = 'processed',
            processed_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = NULL,
            payload = CASE WHEN $2::boolean THEN NULL ELSE payload END,
            payload_purged_at = CASE WHEN $2::boolean THEN now() ELSE payload_purged_at END
        WHERE update_id = $1
        """,
        update_id,
        purge_payload,
    )


async def mark_skipped(conn: asyncpg.Connection, update_id: int) -> None:
    """Неподдерживаемый вид признан обработанным без вызова AI (§3.1)."""
    await conn.execute(
        """
        UPDATE inbox
        SET status = 'skipped',
            processed_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL
        WHERE update_id = $1
        """,
        update_id,
    )


async def requeue(
    conn: asyncpg.Connection,
    update_id: int,
    *,
    error: str,
    backoff_seconds: int,
    max_retries: int,
    now: datetime | None = None,
) -> str:
    """Повтор транзакционного обработчика с backoff; после max_retries — failed.

    Возвращает итоговый статус ('pending' или 'failed'). Доменные эффекты не
    повторяются: обработчик транзакционный и идемпотентный (§3.2).
    """
    moment = now or datetime.now(UTC)
    row = await conn.fetchrow(
        """
        UPDATE inbox
        SET status = CASE WHEN attempts >= $3::int THEN 'failed' ELSE 'pending' END,
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = left($2::text, 2000),
            next_attempt_at = $4::timestamptz + make_interval(secs => $5),
            processed_at = CASE WHEN attempts >= $3::int THEN now() ELSE processed_at END
        WHERE update_id = $1
        RETURNING status
        """,
        update_id,
        error,
        max_retries,
        moment,
        float(backoff_seconds),
    )
    return row["status"] if row else "failed"


async def release_expired_leases(
    conn: asyncpg.Connection, *, now: datetime | None = None
) -> int:
    """Возвращает просроченные lease в очередь (восстановление после сбоя)."""
    moment = now or datetime.now(UTC)
    result = await conn.execute(
        """
        UPDATE inbox
        SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
        WHERE status = 'processing' AND lease_expires_at < $1::timestamptz
        """,
        moment,
    )
    return int(result.split()[-1])


async def pending_stats(conn: asyncpg.Connection) -> dict[str, int]:
    rows: Sequence[asyncpg.Record] = await conn.fetch(
        "SELECT status, count(*)::int AS n FROM inbox GROUP BY status"
    )
    return {r["status"]: r["n"] for r in rows}


def _dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)
