"""Жизненный цикл данных: истечение, удаление и retention (§3.9; задача T18).

Категории и сроки (точка отсчёта — по таблице §3.9):
- незавершённая сессия: истечение после 24 ч неактивности (expires_at),
  удаление контента в течение следующих 24 ч — освободить резерв, отменить
  jobs и outbox, удалить turns и связанные данные;
- завершённые ответы, оценки и отчёт: 30 дней после завершения; очистка
  каждый час; /report после очистки не восстанавливает содержание —
  сохраняется минимальная терминальная запись sessions (параметры, статус,
  счётчики, время) для связи consumed-credit (§3.10); перед удалением turns
  обнуляется current_turn_id;
- неподтверждённый черновик STT: при отказе максимум 1 час — draft_transcript
  удаляется и не используется в отчёте;
- inbox/outbox payload: обнулить до 24 ч после завершения обработки;
  незавершённые содержательные payload не хранятся дольше дедлайна 24 ч;
- метаданные завершённых очередей: 7 дней после терминального статуса
  (журнал ai_calls неизвестных исходов удерживается до сверки, но не дольше
  90 дней — aggregate в daily_usage хранится отдельно);
- события и журнал AI-затрат: 90 дней;
- /delete: блокировка новых действий (часть 2) → физическое удаление данных
  и медиа в течение 24 ч → tombstone (прежний внутренний UUID, 7 дней);
- после restore удаления повторно применяются по tombstones ДО приёма трафика.

Порядок блокировок канонический: users → sessions → credits → turns → jobs
(§2.3); outbox/sessions-контент удаляются после jobs из-за FK jobs→turns.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

from app.config import AppConfig
from app.services import media_service
from app.storage import credits_repo, jobs_repo, outbox_repo, sessions_repo
from contracts.models import SessionStatus

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "cancelled", "expired", "failed")
ACTIVE = tuple(s.value for s in SessionStatus if s.value not in TERMINAL)

BATCH_LIMIT = 100


# ---------------------------------------------------------------------------
# Истечение неактивных сессий (§1.3, §3.9)
# ---------------------------------------------------------------------------


async def expire_sessions(conn: asyncpg.Connection, config: AppConfig) -> int:
    """Неактивная сессия истекает через 24 ч после последнего принятого
    действия пользователя; фоновые повторы срок не продлевают (expires_at
    обновляется только touch_user_action). Резерв освобождается, jobs и
    outbox отменяются (§3.9)."""
    async with conn.transaction():
        rows = await conn.fetch(
            f"""
            SELECT id FROM sessions
            WHERE status NOT IN ({','.join(f"'{s}'" for s in TERMINAL)})
              AND expires_at < now()
            ORDER BY expires_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            BATCH_LIMIT,
        )
        expired = 0
        for row in rows:
            updated = await sessions_repo.transition(
                conn,
                row["id"],
                SessionStatus.expired,
                expected_statuses=tuple(
                    s for s in SessionStatus if s.value not in TERMINAL
                ),
            )
            if updated is None:
                continue  # конкурентный переход — сессия уже обработана
            await credits_repo.release_by_session(conn, row["id"])
            await jobs_repo.cancel_by_session(conn, row["id"])
            await outbox_repo.cancel_pending_by_session(conn, row["id"])
            expired += 1
    return expired


# ---------------------------------------------------------------------------
# Удаление контента терминальных сессий (§3.9, §3.10)
# ---------------------------------------------------------------------------


async def purge_session_content(conn: asyncpg.Connection, config: AppConfig) -> dict[str, int]:
    """Отменённые/истёкшие: контент через 24 ч после завершения; завершённые/
    упавшие: через 30 дней. Удаляются turns (ответы, оценки, черновики) и
    reports; минимальная терминальная запись sessions сохраняется.

    Перед удалением turns обнуляется current_turn_id (§3.10); jobs удаляются
    первыми из-за FK (turn_id, session_id) → turns и каскадируют ai_calls.
    """
    ret = config.settings.retention
    async with conn.transaction():
        rows = await conn.fetch(
            """
            SELECT s.id
            FROM sessions s
            WHERE (
                    (s.status IN ('cancelled', 'expired')
                     AND s.finished_at <= now() - make_interval(hours => $1))
                 OR (s.status IN ('completed', 'failed')
                     AND s.finished_at <= now() - make_interval(days => $2))
                  )
              AND (EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.id)
                   OR EXISTS (SELECT 1 FROM reports r WHERE r.session_id = s.id))
            ORDER BY s.finished_at
            LIMIT $3
            FOR UPDATE OF s SKIP LOCKED
            """,
            ret.cancelled_content_removal_hours,
            ret.completed_content_days,
            BATCH_LIMIT,
        )
        if not rows:
            return {"sessions": 0, "turns": 0, "reports": 0}
        ids = [row["id"] for row in rows]
        # jobs удаляются первыми: FK (turn_id, session_id) → turns (§2.3);
        # ai_calls уходят каскадом с jobs
        await conn.execute("DELETE FROM jobs WHERE session_id = ANY($1::uuid[])", ids)
        # перед удалением turns обнуляется current_turn_id (§3.10)
        await conn.execute(
            "UPDATE sessions SET current_turn_id = NULL "
            "WHERE id = ANY($1::uuid[]) AND current_turn_id IS NOT NULL",
            ids,
        )
        turns_deleted = await conn.execute(
            "DELETE FROM turns WHERE session_id = ANY($1::uuid[])", ids
        )
        reports_deleted = await conn.execute(
            "DELETE FROM reports WHERE session_id = ANY($1::uuid[])", ids
        )
    counts = {
        "sessions": len(ids),
        "turns": _update_count(turns_deleted),
        "reports": _update_count(reports_deleted),
    }
    if counts["sessions"]:
        logger.info(
            "session content purged",
            extra={"sessions": counts["sessions"], "turns": counts["turns"]},
        )
    return counts


async def purge_stt_drafts(conn: asyncpg.Connection, config: AppConfig) -> int:
    """Неподтверждённый черновик STT при отказе хранится максимум 1 час
    (§3.9): draft_transcript удаляется; в отчёт не используется (отчёт
    строится только из подтверждённых ответов)."""
    hours = config.settings.retention.stt_draft_max_hours
    rows = await conn.execute(
        """
        UPDATE turns t
        SET draft_transcript = NULL
        FROM sessions s
        WHERE t.draft_transcript IS NOT NULL
          AND t.session_id = s.id
          AND s.last_user_action_at <= now() - make_interval(hours => $1)
        """,
        hours,
    )
    return _update_count(rows)


# ---------------------------------------------------------------------------
# Очереди: payload и метаданные (§3.9)
# ---------------------------------------------------------------------------


async def purge_queue_payloads(conn: asyncpg.Connection, config: AppConfig) -> dict[str, int]:
    """Обнуление payload: inbox/outbox — до 24 ч после завершения обработки.
    Незавершённые содержательные payload не хранятся дольше дедлайна 24 ч:
    залежавшиеся pending-строки признаются просроченными (inbox → skipped,
    outbox → cancelled) и обнуляются."""
    hours = config.settings.retention.queue_payload_nullify_hours
    inbox_done = await conn.execute(
        """
        UPDATE inbox
        SET payload = NULL, payload_purged_at = now()
        WHERE payload IS NOT NULL
          AND status IN ('processed', 'skipped', 'failed')
          AND processed_at <= now() - make_interval(hours => $1)
        """,
        hours,
    )
    inbox_stale = await conn.execute(
        """
        UPDATE inbox
        SET status = 'skipped', processed_at = now(),
            payload = NULL, payload_purged_at = now(),
            lease_owner = NULL, lease_expires_at = NULL
        WHERE payload IS NOT NULL
          AND status IN ('pending', 'processing')
          AND received_at <= now() - make_interval(hours => $1)
          AND (lease_expires_at IS NULL OR lease_expires_at < now())
        """,
        hours,
    )
    outbox_done = await conn.execute(
        """
        UPDATE outbox
        SET payload = NULL
        WHERE payload IS NOT NULL
          AND state IN ('delivered', 'failed', 'cancelled')
          AND COALESCE(delivered_at, created_at) <= now() - make_interval(hours => $1)
        """,
        hours,
    )
    outbox_stale = await conn.execute(
        """
        UPDATE outbox
        SET state = 'cancelled', payload = NULL,
            last_error = COALESCE(last_error, 'retention_deadline')
        WHERE payload IS NOT NULL
          AND state IN ('pending', 'sending')
          AND created_at <= now() - make_interval(hours => $1)
        """,
        hours,
    )
    return {
        "inbox_payloads": _update_count(inbox_done) + _update_count(inbox_stale),
        "outbox_payloads": _update_count(outbox_done) + _update_count(outbox_stale),
    }


async def purge_queue_metadata(conn: asyncpg.Connection, config: AppConfig) -> dict[str, int]:
    """Метаданные завершённых очередей — 7 дней после терминального статуса
    (§3.9). Подтверждённые question IDs (turns.delivered_message_id) живут,
    пока активна сессия: turns здесь не удаляются.

    Журнал ai_calls неизвестных исходов удерживается до сверки: jobs с
    unknown-тарификацией не удаляются, пока вызову не исполнится 90 дней
    (§3.10: неизвестный результат учитывается как unknown_usd до сверки)."""
    days = config.settings.retention.queue_metadata_days
    ai_days = config.settings.retention.ai_calls_days
    jobs_deleted = await conn.execute(
        """
        DELETE FROM jobs
        WHERE state IN ('done', 'failed', 'cancelled')
          AND finished_at <= now() - make_interval(days => $1)
          AND NOT EXISTS (
              SELECT 1 FROM ai_calls ac
              WHERE ac.job_id = jobs.id
                AND ac.result_state = 'unknown'
                AND ac.created_at > now() - make_interval(days => $2)
          )
        """,
        days,
        ai_days,
    )
    inbox_deleted = await conn.execute(
        """
        DELETE FROM inbox
        WHERE status IN ('processed', 'skipped', 'failed')
          AND processed_at <= now() - make_interval(days => $1)
        """,
        days,
    )
    outbox_deleted = await conn.execute(
        """
        DELETE FROM outbox
        WHERE state IN ('delivered', 'failed', 'cancelled')
          AND COALESCE(delivered_at, created_at) <= now() - make_interval(days => $1)
        """,
        days,
    )
    tokens_deleted = await conn.execute(
        """
        DELETE FROM ui_tokens
        WHERE (consumed_at IS NOT NULL AND consumed_at <= now() - make_interval(days => $1))
           OR expires_at <= now() - make_interval(days => $1)
        """,
        days,
    )
    return {
        "jobs": _update_count(jobs_deleted),
        "inbox": _update_count(inbox_deleted),
        "outbox": _update_count(outbox_deleted),
        "ui_tokens": _update_count(tokens_deleted),
    }


async def purge_events_and_costs(conn: asyncpg.Connection, config: AppConfig) -> dict[str, int]:
    """События и журнал AI-затрат — 90 дней (§3.9): строки удаляются;
    суточные агрегаты затрат старше срока также удаляются (без связей
    с человеком они уже не содержат)."""
    events_days = config.settings.retention.events_days
    ai_days = config.settings.retention.ai_calls_days
    events_deleted = await conn.execute(
        "DELETE FROM events WHERE created_at <= now() - make_interval(days => $1)",
        events_days,
    )
    ai_deleted = await conn.execute(
        """
        DELETE FROM ai_calls
        WHERE created_at <= now() - make_interval(days => $1)
        """,
        ai_days,
    )
    usage_deleted = await conn.execute(
        """
        DELETE FROM daily_usage
        WHERE day < (now() - make_interval(days => $1))::date
        """,
        ai_days,
    )
    return {
        "events": _update_count(events_deleted),
        "ai_calls": _update_count(ai_deleted),
        "daily_usage": _update_count(usage_deleted),
    }


async def purge_tombstones(conn: asyncpg.Connection, config: AppConfig) -> int:
    """Служебный tombstone хранится 7 дней (§3.9)."""
    rows = await conn.execute("DELETE FROM deletion_tombstones WHERE expires_at <= now()")
    return _update_count(rows)


# ---------------------------------------------------------------------------
# /delete: физическое удаление и tombstones (§3.9, AT13)
# ---------------------------------------------------------------------------


async def process_user_deletions(
    conn: asyncpg.Connection, config: AppConfig, media_dir: Path
) -> int:
    """/delete: новые действия заблокированы и очереди отменены (часть 2);
    здесь — удаление данных и медиа, затем пользователя; tombstone содержит
    только прежний внутренний UUID и хранится 7 дней. Срок — не более 24 ч
    от запроса (deletion_deadline_hours), sweep выполняется чаще."""
    users = await conn.fetch(
        """
        SELECT id FROM users
        WHERE deletion_requested_at IS NOT NULL
          AND deletion_requested_at <= now()
        ORDER BY deletion_requested_at
        LIMIT $1
        """,
        BATCH_LIMIT,
    )
    deleted = 0
    for user in users:
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1 FOR UPDATE", user["id"]
            )
            if locked is None:
                continue  # уже удалён конкурентным sweep
            keys = await conn.fetch(
                """
                SELECT j.media_storage_key
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE s.user_id = $1 AND j.media_storage_key IS NOT NULL
                """,
                user["id"],
            )
            await conn.execute(
                """
                INSERT INTO deletion_tombstones (user_id, expires_at)
                VALUES ($1, now() + make_interval(days => $2))
                ON CONFLICT (user_id) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at
                """,
                user["id"],
                config.settings.retention.tombstone_days,
            )
            # Отмена активных очередей до удаления (порядок §2.3: sessions →
            # credits → jobs → outbox); строки удаляются каскадом с users.
            active = await conn.fetch(
                f"""
                SELECT id FROM sessions
                WHERE user_id = $1 AND status NOT IN ({','.join(f"'{s}'" for s in TERMINAL)})
                FOR UPDATE
                """,
                user["id"],
            )
            for row in active:
                await credits_repo.release_by_session(conn, row["id"])
                await jobs_repo.cancel_by_session(conn, row["id"])
                await outbox_repo.cancel_pending_by_session(conn, row["id"])
            # ожидания удалённого профиля не обрабатываются: сообщения
            # удалённого пользователя не появляются снова (AT06)
            await conn.execute(
                """
                UPDATE inbox
                SET status = 'skipped',
                    processed_at = COALESCE(processed_at, now()),
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE user_id = $1 AND status IN ('pending', 'processing')
                """,
                user["id"],
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user["id"])
        # Медиа удаляются после commit: файл не нужен для целостности БД
        for key in keys:
            media_service.remove_file(_media_path(media_dir, key["media_storage_key"]))
        deleted += 1
        logger.info("user deleted by request", extra={"tombstone": str(user["id"])})
    return deleted


async def reapply_tombstones(conn: asyncpg.Connection) -> int:
    """После восстановления более старого backup удаления повторяются по
    tombstones ДО приёма трафика (§3.9, AT13): восстановленный профиль,
    совпадающий с tombstone, удаляется снова вместе с данными."""
    rows = await conn.execute(
        """
        DELETE FROM users
        WHERE id IN (SELECT user_id FROM deletion_tombstones)
        """
    )
    removed = _update_count(rows)
    if removed:
        logger.warning("restore re-deleted profiles by tombstones", extra={"count": removed})
    return removed


async def purge_inbox_of_deleted_users(conn: asyncpg.Connection) -> int:
    """Восстановленные inbox-строки сверяются с tombstones до обработки
    (§3.9): обновления удалённого профиля пропускаются без доменных эффектов."""
    rows = await conn.execute(
        """
        UPDATE inbox
        SET status = 'skipped',
            processed_at = COALESCE(processed_at, now()),
            lease_owner = NULL, lease_expires_at = NULL
        WHERE status IN ('pending', 'processing')
          AND user_id IN (SELECT user_id FROM deletion_tombstones)
        """
    )
    return _update_count(rows)


# ---------------------------------------------------------------------------
# Оркестрация sweep-циклов (§3.9)
# ---------------------------------------------------------------------------


async def run_session_sweep(pool: asyncpg.Pool, config: AppConfig, media_dir: Path) -> dict:
    """Быстрый цикл (каждые session_sweep_interval_minutes): истечение
    неактивных сессий, черновики STT, физические удаления /delete."""
    async with pool.acquire() as conn:
        expired = await expire_sessions(conn, config)
        drafts = await purge_stt_drafts(conn, config)
        deleted = await process_user_deletions(conn, config, media_dir)
    return {"expired_sessions": expired, "stt_drafts": drafts, "users_deleted": deleted}


async def run_retention_sweep(pool: asyncpg.Pool, config: AppConfig) -> dict:
    """Полный цикл каждый час (§3.9: «очистка каждый час»): контент
    терминальных сессий, payload и метаданные очередей, события/затраты,
    tombstones, восстановленные inbox удалённых профилей."""
    async with pool.acquire() as conn:
        content = await purge_session_content(conn, config)
        payloads = await purge_queue_payloads(conn, config)
        metadata = await purge_queue_metadata(conn, config)
        events = await purge_events_and_costs(conn, config)
        tombstones = await purge_tombstones(conn, config)
        restored_inbox = await purge_inbox_of_deleted_users(conn)
    return {
        "content": content,
        "payloads": payloads,
        "metadata": metadata,
        "events": events,
        "tombstones": tombstones,
        "restored_inbox_skipped": restored_inbox,
    }


def _media_path(media_dir: Path, key: str) -> Path:
    """media_storage_key — имя файла во временном каталоге (32 символа, §3.5);
    путь собирается заново, чтобы исключить выход за пределы каталога."""
    name = Path(key).name
    return media_dir / name


def _update_count(result: str) -> int:
    """asyncpg execute возвращает 'UPDATE n' / 'DELETE n'."""
    parts = result.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0
