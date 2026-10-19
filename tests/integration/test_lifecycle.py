"""Жизненный цикл: истечение, retention и удаление (§3.9, §3.10; задача T18).

Проверяются: истечение неактивной сессии с освобождением резерва; удаление
контента отменённых/истёкших (24 ч) и завершённых (30 дней) сессий с
сохранением минимальной терминальной записи; /report после очистки не
восстанавливает содержание; черновики STT; обнуление payload и дедлайн 24 ч
для незавершённых; метаданные очередей (7 дней) с удержанием unknown-затрат
до сверки; события/журнал затрат (90 дней); tombstones (7 дней); физическое
/delete-удаление с медиа и повторное применение tombstones после restore;
поздние результаты и старые кнопки не восстанавливают терминальные данные
(AT06).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.services import lifecycle
from app.storage import inbox_repo
from app.telegram import messages

pytestmark = pytest.mark.asyncio

ANSWER = "Учусь на третьем курсе, пишу учебный проект по анализу данных."


async def _expire_now(harness, session_id) -> None:
    async with harness.pool.acquire() as conn:
        # сессия «неактивна 24 ч»: created_at старше expires_at (CHECK
        # sessions_expiry_order), expires_at в прошлом
        await conn.execute(
            "UPDATE sessions SET created_at = now() - interval '25 hours', "
            "last_user_action_at = now() - interval '25 hours', "
            "expires_at = now() - interval '1 minute' WHERE id = $1",
            session_id,
        )


# ---------------------------------------------------------------------------
# Истечение неактивной сессии (§1.3, §3.9)
# ---------------------------------------------------------------------------


async def test_expire_inactive_session_releases_reserve(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    await _expire_now(harness, session["id"])

    async with harness.pool.acquire() as conn:
        expired = await lifecycle.expire_sessions(conn, harness.config)
    assert expired == 1

    row = await harness.fetch_session(session["id"])
    assert row["status"] == "expired"
    assert row["finished_at"] is not None
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"  # резерв освобождён
    assert credits[0]["reserved_session_id"] is None


async def test_expire_cancels_jobs_and_pending_outbox(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    # assess-задание создано, но ещё не обработано
    await _expire_now(harness, session["id"])
    async with harness.pool.acquire() as conn:
        await lifecycle.expire_sessions(conn, harness.config)

    jobs = await harness.fetch_jobs_by_kind("assess")
    assert jobs and all(j["state"] == "cancelled" for j in jobs)
    # поздний результат не восстанавливает сессию: задания не claim-ятся
    processed = await harness.run_jobs()
    assert processed == 0
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "expired"


async def test_expired_session_old_buttons_are_stale(harness) -> None:
    """AT06: старые кнопки finish/cancel не восстанавливают терминальное
    состояние."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    finish_button = await harness.button_for("finish_session")
    cancel_button = await harness.button_for("cancel_session")
    await _expire_now(harness, session["id"])
    async with harness.pool.acquire() as conn:
        await lifecycle.expire_sessions(conn, harness.config)

    await harness.click(finish_button)
    await harness.click(cancel_button)
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "expired"  # не восстановлена


# ---------------------------------------------------------------------------
# Удаление контента по срокам (§3.9, §3.10)
# ---------------------------------------------------------------------------


async def test_completed_content_purged_after_30_days(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"

    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET finished_at = now() - interval '31 days' WHERE id = $1",
            session["id"],
        )
        counts = await lifecycle.purge_session_content(conn, harness.config)

    assert counts["sessions"] == 1
    assert counts["turns"] >= 1
    assert counts["reports"] == 1
    # минимальная терминальная запись сохранена (§3.10)
    row = await harness.fetch_session(session["id"])
    assert row is not None
    assert row["status"] == "completed"
    assert row["current_turn_id"] is None  # обнулён до удаления turns
    assert row["role_id"] == "python" and row["level"] == "junior"
    assert await harness.fetch_turns(session["id"]) == []
    assert await harness.fetch_report(session["id"]) is None
    # consumed-credit связь сохранена
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "consumed"
    assert credits[0]["consumed_session_id"] == session["id"]


async def test_report_command_after_purge_does_not_restore(harness) -> None:
    """/report после очистки не восстанавливает содержание (§3.9)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET finished_at = now() - interval '31 days' WHERE id = $1",
            session["id"],
        )
        await lifecycle.purge_session_content(conn, harness.config)

    await harness.send_text("/report")
    assert any(messages.REPORT_EXPIRED in t for t in harness.bot.texts())
    assert await harness.fetch_report(session["id"]) is None


async def test_cancelled_content_purged_after_24_hours(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    await harness.cancel_active()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "cancelled"

    async with harness.pool.acquire() as conn:
        # сразу после отмены контент ещё хранится (окно 24 ч)
        counts = await lifecycle.purge_session_content(conn, harness.config)
        assert counts["sessions"] == 0
        await conn.execute(
            "UPDATE sessions SET finished_at = now() - interval '25 hours' WHERE id = $1",
            session["id"],
        )
        counts = await lifecycle.purge_session_content(conn, harness.config)
    assert counts["sessions"] == 1
    assert await harness.fetch_turns(session["id"]) == []
    assert await harness.fetch_session(session["id"]) is not None


async def test_stt_draft_purged_after_one_hour(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE turns SET draft_transcript = 'черновик' WHERE session_id = $1",
            session["id"],
        )
        purged = await lifecycle.purge_stt_drafts(conn, harness.config)
        assert purged == 0  # свежий черновик не удаляется
        await conn.execute(
            "UPDATE sessions SET last_user_action_at = now() - interval '2 hours' "
            "WHERE id = $1",
            session["id"],
        )
        purged = await lifecycle.purge_stt_drafts(conn, harness.config)
    assert purged == 1
    turns = await harness.fetch_turns(session["id"])
    assert all(t["draft_transcript"] is None for t in turns)


# ---------------------------------------------------------------------------
# Очереди: payload и метаданные (§3.9)
# ---------------------------------------------------------------------------


async def test_queue_payloads_nullified_after_24h(harness) -> None:
    await harness.complete_entry()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE inbox SET processed_at = now() - interval '25 hours' "
            "WHERE status = 'processed'"
        )
        await conn.execute(
            "UPDATE outbox SET delivered_at = now() - interval '25 hours' "
            "WHERE state = 'delivered'"
        )
        counts = await lifecycle.purge_queue_payloads(conn, harness.config)
        inbox_left = await conn.fetchval(
            "SELECT count(*) FROM inbox WHERE payload IS NOT NULL AND status <> 'pending'"
        )
        outbox_left = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE payload IS NOT NULL AND state = 'delivered'"
        )
        purged_marks = await conn.fetchval(
            "SELECT count(*) FROM inbox WHERE payload_purged_at IS NOT NULL"
        )
    assert counts["inbox_payloads"] >= 1
    assert counts["outbox_payloads"] >= 1
    assert inbox_left == 0 and outbox_left == 0
    assert purged_marks >= 1


async def test_stale_pending_outbox_cancelled_at_deadline(harness) -> None:
    """Незавершённые содержательные payload не хранятся дольше дедлайна 24 ч."""
    await harness.complete_entry()
    user = await harness.fetch_user(555)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO outbox (unique_key, user_id, chat_id, kind, seq, payload,
                                created_at)
            VALUES ('stale-test', $1, 555, 'text',
                    (SELECT COALESCE(max(seq), 0) + 1 FROM outbox WHERE chat_id = 555),
                    '{"text": "old"}'::jsonb, now() - interval '25 hours')
            """,
            user["id"],
        )
        await lifecycle.purge_queue_payloads(conn, harness.config)
        row = await conn.fetchrow(
            "SELECT state, payload, last_error FROM outbox WHERE unique_key = 'stale-test'"
        )
    assert row["state"] == "cancelled"
    assert row["payload"] is None
    assert row["last_error"] == "retention_deadline"


async def test_queue_metadata_removed_after_7_days(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()  # assess-задание done, ai_calls success
    await harness.drain_outbox()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE inbox SET processed_at = now() - interval '8 days' "
            "WHERE status IN ('processed', 'skipped')"
        )
        await conn.execute(
            "UPDATE outbox SET delivered_at = now() - interval '8 days' "
            "WHERE state = 'delivered'"
        )
        await conn.execute(
            "UPDATE jobs SET finished_at = now() - interval '8 days' "
            "WHERE state IN ('done', 'failed', 'cancelled')"
        )
        await conn.execute(
            "UPDATE ui_tokens SET created_at = now() - interval '9 days', "
            "consumed_at = now() - interval '8 days' "
            "WHERE consumed_at IS NOT NULL"
        )
        counts = await lifecycle.purge_queue_metadata(conn, harness.config)
        ai_calls_left = await conn.fetchval("SELECT count(*) FROM ai_calls")
    assert counts["jobs"] >= 1
    assert counts["inbox"] >= 1
    assert counts["outbox"] >= 1
    assert counts["ui_tokens"] >= 1
    assert ai_calls_left == 0  # success-журнал удалён каскадом с jobs
    assert (await harness.fetch_session(session["id"])) is not None


async def test_unknown_cost_journal_held_until_reconciliation(harness) -> None:
    """Неизвестный результат учитывается как unknown_usd до сверки (§3.10):
    jobs с unknown ai_calls не удаляются 7-дневной очисткой метаданных,
    но не хранятся дольше 90 дней."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()
    async with harness.pool.acquire() as conn:
        await conn.execute("UPDATE ai_calls SET result_state = 'unknown'")
        await conn.execute(
            "UPDATE jobs SET finished_at = now() - interval '8 days' "
            "WHERE state IN ('done', 'failed', 'cancelled')"
        )
        counts = await lifecycle.purge_queue_metadata(conn, harness.config)
        assert counts["jobs"] == 0
        held = await conn.fetchval("SELECT count(*) FROM ai_calls WHERE result_state = 'unknown'")
        assert held >= 1
        # 90 дней — предельный срок журнала (§3.9)
        await conn.execute(
            "UPDATE ai_calls SET created_at = now() - interval '91 days'"
        )
        counts = await lifecycle.purge_queue_metadata(conn, harness.config)
        assert counts["jobs"] >= 1
        left = await conn.fetchval("SELECT count(*) FROM ai_calls")
    assert left == 0


async def test_events_and_costs_removed_after_90_days(harness) -> None:
    await harness.complete_entry()
    async with harness.pool.acquire() as conn:
        events_before = await conn.fetchval("SELECT count(*) FROM events")
        usage_before = await conn.fetchval("SELECT count(*) FROM daily_usage")
        assert events_before >= 1
        await conn.execute(
            "UPDATE events SET created_at = now() - interval '91 days'"
        )
        await conn.execute("UPDATE daily_usage SET day = day - 91")
        counts = await lifecycle.purge_events_and_costs(conn, harness.config)
    assert counts["events"] == events_before
    assert counts["daily_usage"] == usage_before
    async with harness.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM events") == 0


async def test_tombstones_expire_after_7_days(harness) -> None:
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO deletion_tombstones (user_id, deleted_at, expires_at) "
            "VALUES ($1, now() - interval '8 days', now() - interval '1 minute')",
            uuid.uuid4(),
        )
        removed = await lifecycle.purge_tombstones(conn, harness.config)
        left = await conn.fetchval("SELECT count(*) FROM deletion_tombstones")
    assert removed == 1 and left == 0


# ---------------------------------------------------------------------------
# /delete: физическое удаление, tombstone, restore (§3.9, AT13)
# ---------------------------------------------------------------------------


async def test_user_deletion_removes_data_media_and_creates_tombstone(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()  # assess-задание сессии существует
    user = await harness.fetch_user(555)
    media_dir: Path = harness.config.env.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    media_file = media_dir / ("a1" * 16)
    media_file.write_bytes(b"ogg-data")
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET media_storage_key = $2 WHERE session_id = $1",
            session["id"],
            media_file.name,
        )
        await conn.execute(
            "UPDATE users SET deletion_requested_at = now() WHERE id = $1", user["id"]
        )
        deleted = await lifecycle.process_user_deletions(conn, harness.config, media_dir)

    assert deleted == 1
    assert not media_file.exists()  # медиа удалены
    async with harness.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM users") == 0
        assert await conn.fetchval("SELECT count(*) FROM sessions") == 0
        assert await conn.fetchval("SELECT count(*) FROM credits") == 0
        tombstone = await conn.fetchrow("SELECT * FROM deletion_tombstones")
        inbox_user = await conn.fetchval("SELECT user_id FROM inbox LIMIT 1")
    assert tombstone is not None
    assert tombstone["user_id"] == user["id"]  # только прежний внутренний UUID
    assert tombstone["expires_at"] > tombstone["deleted_at"]
    assert inbox_user is None  # inbox сохранён, ссылка обнулена


async def test_reapply_tombstones_after_restore(harness) -> None:
    """Restore более старого backup: профиль из tombstone удаляется повторно
    до приёма трафика (§3.9)."""
    await harness.complete_entry()
    user = await harness.fetch_user(555)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET deletion_requested_at = now() WHERE id = $1", user["id"]
        )
        await lifecycle.process_user_deletions(
            conn, harness.config, harness.config.env.media_dir
        )
        # эмуляция restore: профиль и сессия возвращены из старой копии
        await conn.execute(
            "INSERT INTO users (id, telegram_user_id, chat_id, ui_state) "
            "VALUES ($1, 555, 555, 'idle')",
            user["id"],
        )
        removed = await lifecycle.reapply_tombstones(conn)
        left = await conn.fetchval("SELECT count(*) FROM users")
    assert removed == 1
    assert left == 0


async def test_restored_inbox_of_tombstoned_user_skipped(harness) -> None:
    """Восстановленный inbox сверяется с tombstones до обработки (§3.9).

    Эмуляция restore из более старой копии: профиль и его pending-обновления
    вернулись, tombstone из защищённого журнала удаления сохранился. Порядок
    до приёма трафика: сначала inbox сверяется с tombstones (user_id ещё не
    обнулён), затем профили удаляются повторно."""
    await harness.complete_entry()
    user = await harness.fetch_user(555)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO deletion_tombstones (user_id, expires_at) "
            "VALUES ($1, now() + interval '7 days')",
            user["id"],
        )
        await inbox_repo.insert_update(
            conn,
            update_id=999_001,
            kind="message",
            payload={
                "update_id": 999_001,
                "message": {
                    "message_id": 1,
                    "date": 1760000000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 555},
                    "text": "/start",
                },
            },
            initial_status="pending",
        )
        await conn.execute(
            "UPDATE inbox SET user_id = $1 WHERE update_id = 999001", user["id"]
        )

        skipped = await lifecycle.purge_inbox_of_deleted_users(conn)
        removed = await lifecycle.reapply_tombstones(conn)
        row = await conn.fetchrow(
            "SELECT status, user_id FROM inbox WHERE update_id = 999001"
        )

    assert skipped >= 1
    assert removed == 1
    assert row["status"] == "skipped"
    assert row["user_id"] is None  # профиль удалён повторно
    # обработчик не вызывался: новых отправок нет
    assert all("999001" not in str(p) for p in harness.bot.sent)


async def test_worker_skips_claimed_update_of_tombstoned_user(harness) -> None:
    """Защита на уровне worker: claim-нутое обновление профиля из tombstone
    признаётся skipped без доменных эффектов."""
    await harness.complete_entry()
    user = await harness.fetch_user(555)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO deletion_tombstones (user_id, expires_at) "
            "VALUES ($1, now() + interval '7 days')",
            user["id"],
        )
        await inbox_repo.insert_update(
            conn,
            update_id=999_002,
            kind="message",
            payload={
                "update_id": 999_002,
                "message": {
                    "message_id": 2,
                    "date": 1760000000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 555},
                    "text": "/start",
                },
            },
            initial_status="pending",
        )
        await conn.execute(
            "UPDATE inbox SET user_id = $1 WHERE update_id = 999002", user["id"]
        )
        batch = await inbox_repo.claim_batch(conn, owner="test", limit=10)

    sent_before = len(harness.bot.sent)
    item = next(i for i in batch if i["update_id"] == 999_002)
    await harness.inbox._process(item)

    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM inbox WHERE update_id = 999002"
        )
    assert row["status"] == "skipped"
    assert len(harness.bot.sent) == sent_before


async def test_deleted_user_old_buttons_do_not_recreate_data(harness) -> None:
    """AT06: удалённые данные и сообщения не появляются снова."""
    await harness.complete_entry()
    finish_button = await harness.button_for("finish_session")
    user = await harness.fetch_user(555)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET deletion_requested_at = now() WHERE id = $1", user["id"]
        )
        await lifecycle.process_user_deletions(
            conn, harness.config, harness.config.env.media_dir
        )

    sent_before = len(harness.bot.sent)
    await harness.click(finish_button)
    async with harness.pool.acquire() as conn:
        users = await conn.fetchval("SELECT count(*) FROM users")
        sessions = await conn.fetchval("SELECT count(*) FROM sessions")
        outbox = await conn.fetchval("SELECT count(*) FROM outbox")
    assert users == 0 and sessions == 0 and outbox == 0
    assert len(harness.bot.sent) == sent_before


# ---------------------------------------------------------------------------
# Оркестрация sweep-циклов (§3.9)
# ---------------------------------------------------------------------------


async def test_session_sweep_cycle(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    await _expire_now(harness, session["id"])
    result = await lifecycle.run_session_sweep(
        harness.pool, harness.config, harness.config.env.media_dir
    )
    assert result["expired_sessions"] == 1
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "expired"


async def test_retention_sweep_cycle_is_safe_on_empty_db(harness) -> None:
    result = await lifecycle.run_retention_sweep(harness.pool, harness.config)
    assert result["restored_inbox_skipped"] == 0
    assert result["content"]["sessions"] == 0


async def test_run_session_sweep_serializable_shape(harness) -> None:
    """Результат sweep — только счётчики (в логи не попадает содержимое)."""
    result = await lifecycle.run_session_sweep(
        harness.pool, harness.config, harness.config.env.media_dir
    )
    assert json.dumps(result).count('"') >= 6  # сериализуемо
    assert set(result) == {"expired_sessions", "stt_drafts", "users_deleted"}
