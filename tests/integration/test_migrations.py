"""Миграции и ограничения модели данных (§2.3, задача T03)."""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from app.storage.migrate import MigrationError, run_migrations

TABLES_EXPECTED = {
    "users",
    "sessions",
    "turns",
    "reports",
    "credits",
    "inbox",
    "jobs",
    "outbox",
    "ui_tokens",
    "events",
    "daily_usage",
    "ai_calls",
    "deletion_tombstones",
    "payments",
}


async def test_all_14_tables_created(db: asyncpg.Connection) -> None:
    rows = await db.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    tables = {r["tablename"] for r in rows}
    assert TABLES_EXPECTED <= tables
    assert "schema_migrations" in tables
    # лишних таблиц нет
    assert tables == TABLES_EXPECTED | {"schema_migrations"}


async def test_migrations_are_idempotent(db_url: str) -> None:
    applied = await run_migrations(db_url)
    assert applied == []  # уже применены в conftest


async def test_tampered_migration_blocked(db_url: str, tmp_path: Path) -> None:
    """Изменение применённой миграции блокирует запуск — создавайте новую."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "9001_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS _mig_test (id int);", encoding="utf-8"
    )
    assert await run_migrations(db_url, migrations) == ["9001_test.sql"]
    (migrations / "9001_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS _mig_test (id bigint);", encoding="utf-8"
    )
    with pytest.raises(MigrationError):
        await run_migrations(db_url, migrations)
    await db_cleanup_test_table(db_url)


async def db_cleanup_test_table(db_url: str) -> None:
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("DROP TABLE IF EXISTS _mig_test")
        await conn.execute("DELETE FROM schema_migrations WHERE filename = '9001_test.sql'")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Вспомогательные вставки
# ---------------------------------------------------------------------------


async def make_user(db: asyncpg.Connection, telegram_id: int = 555) -> uuid.UUID:
    return await db.fetchval(
        """
        INSERT INTO users (telegram_user_id, chat_id, ui_state)
        VALUES ($1, $1, 'new')
        RETURNING id
        """,
        telegram_id,
    )


async def make_session(
    db: asyncpg.Connection,
    user_id: uuid.UUID,
    *,
    status: str = "awaiting_answer",
    finished: bool | None = None,
) -> uuid.UUID:
    if finished is None:
        finished = status in ("completed", "cancelled", "expired", "failed")
    return await db.fetchval(
        """
        INSERT INTO sessions (user_id, role_id, level, format, status,
                              catalog_version, prompt_version, current_difficulty,
                              expires_at, finished_at)
        VALUES ($1, 'python', 'junior', 'text', $2, '2.0', '2.0', 2,
                now() + interval '24 hours', CASE WHEN $3::boolean THEN now() ELSE NULL END)
        RETURNING id
        """,
        user_id,
        status,
        finished,
    )


# ---------------------------------------------------------------------------
# Ограничения целостности
# ---------------------------------------------------------------------------


async def test_one_active_session_per_user(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    await make_session(db, user_id, status="awaiting_answer")
    with pytest.raises(asyncpg.UniqueViolationError):
        await make_session(db, user_id, status="preparing")


async def test_active_plus_terminal_sessions_allowed(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    await make_session(db, user_id, status="completed")
    await make_session(db, user_id, status="cancelled")
    await make_session(db, user_id, status="awaiting_answer")  # не нарушает


async def test_terminal_session_requires_finished_at(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    with pytest.raises(asyncpg.CheckViolationError):
        await make_session(db, user_id, status="completed", finished=False)


async def test_one_trial_credit_per_user(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    await db.execute(
        "INSERT INTO credits (user_id, source, state) VALUES ($1, 'trial', 'available')",
        user_id,
    )
    # UNIQUE(user_id) для source=trial (§3.8): вторая попытка не создаётся
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO credits (user_id, source, state) VALUES ($1, 'trial', 'available')",
            user_id,
        )


async def test_credit_state_session_pairs(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id)
    # reserved без reserved_session_id запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO credits (user_id, source, state) VALUES ($1, 'trial', 'reserved')",
            user_id,
        )
    # reserved с сессией — корректно
    await db.execute(
        "INSERT INTO credits (user_id, source, state, reserved_session_id) "
        "VALUES ($1, 'trial', 'reserved', $2)",
        user_id,
        session_id,
    )
    # consumed без consumed_session_id запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO credits (user_id, source, state) VALUES ($1, 'grant', 'consumed') "
            "ON CONFLICT DO NOTHING",
            user_id,
        )


async def test_payment_credit_requires_payment_fields(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO credits (user_id, source, state) VALUES ($1, 'payment', 'available')",
            user_id,
        )
    payment_id = await db.fetchval(
        """
        INSERT INTO payments (user_id, product_id, units, xtr_amount, invoice_payload,
                              status, terms_version, expires_at)
        VALUES ($1, 'pack5', 5, 500, 'ord_test_1', 'pending', '2.0', now() + interval '30 min')
        RETURNING id
        """,
        user_id,
    )
    await db.execute(
        "INSERT INTO credits (user_id, source, state, payment_id, unit_index) "
        "VALUES ($1, 'payment', 'available', $2, 1)",
        user_id,
        payment_id,
    )
    # UNIQUE(payment_id, unit_index) защищает от двойного начисления единицы (§3.8)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO credits (user_id, source, state, payment_id, unit_index) "
            "VALUES ($1, 'payment', 'available', $2, 1)",
            user_id,
            payment_id,
        )


async def test_payments_charge_id_unique_and_units_match(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    await db.execute(
        """
        INSERT INTO payments (user_id, product_id, units, xtr_amount, invoice_payload,
                              charge_id, status, terms_version, expires_at, paid_at)
        VALUES ($1, 'single', 1, 75, 'ord_a', 'tg_charge_1', 'paid', '2.0', now(), now())
        """,
        user_id,
    )
    # повтор того же charge_id не проходит — двойное начисление исключено (§3.8)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO payments (user_id, product_id, units, xtr_amount, invoice_payload,
                                  charge_id, status, terms_version, expires_at, paid_at)
            VALUES ($1, 'single', 1, 75, 'ord_b', 'tg_charge_1', 'paid', '2.0', now(), now())
            """,
            user_id,
        )
    # units обязаны соответствовать продукту
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO payments (user_id, product_id, units, xtr_amount, invoice_payload,
                                  status, terms_version, expires_at)
            VALUES ($1, 'pack5', 3, 500, 'ord_c', 'pending', '2.0', now())
            """,
            user_id,
        )


async def test_turns_constraints(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id)

    async def add_turn(
        ordinal: int,
        kind: str = "main",
        main_index: int | None = None,
        parent: uuid.UUID | None = None,
        answer: str | None = None,
        skipped: bool = False,
        question_type: str = "technical",
        difficulty: int | None = 2,
    ) -> uuid.UUID:
        return await db.fetchval(
            """
            INSERT INTO turns (session_id, ordinal, kind, question_type, main_index,
                               parent_turn_id, topic, question_text, rubric, difficulty,
                               catalog_entry_id, answer_text, answer_received_at, skipped)
            VALUES ($1, $2, $3, $4, $5, $6, 'collections', 'Вопрос?', '{}'::jsonb, $7,
                    'python-junior-tech-collections', $8,
                    CASE WHEN $8::text IS NULL THEN NULL ELSE now() END, $9)
            RETURNING id
            """,
            session_id,
            ordinal,
            kind,
            question_type,
            main_index,
            parent,
            difficulty,
            answer,
            skipped,
        )

    t1 = await add_turn(1, main_index=1, question_type="hr", difficulty=None)
    t2 = await add_turn(2, main_index=2, answer="Ответ пользователя")

    # UNIQUE(session_id, ordinal)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_turn(2, main_index=3)
    # UNIQUE(session_id, main_index) для kind=main
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_turn(3, main_index=2)
    # main без main_index запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await add_turn(3)
    # follow_up без parent запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await add_turn(3, kind="follow_up", main_index=None)
    # одно уточнение на parent
    f1 = await add_turn(3, kind="follow_up", parent=t2)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_turn(4, kind="follow_up", parent=t2)
    # пропуск: answer NULL, skipped=true — допустим; skipped с ответом — нет
    await add_turn(5, main_index=4, skipped=True, answer=None)
    with pytest.raises(asyncpg.CheckViolationError):
        await add_turn(6, main_index=5, skipped=True, answer="текст")
    # ответ 4001 символ запрещён на уровне БД
    with pytest.raises(asyncpg.CheckViolationError):
        await add_turn(6, main_index=5, answer="а" * 4001)
    # вопрос длиннее 700 запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO turns (session_id, ordinal, kind, question_type, main_index, topic,
                               question_text, rubric, difficulty, catalog_entry_id)
            VALUES ($1, 9, 'main', 'technical', 6, 'metrics', $2, '{}'::jsonb, 2, 'x')
            """,
            session_id,
            "в" * 701,
        )
    assert f1 and t1


async def test_cross_session_turn_fk_rejected(db: asyncpg.Connection) -> None:
    """Составной FK (turn_id, session_id) предотвращает привязку вопроса
    другой сессии (§2.3)."""
    user_id = await make_user(db)
    session_a = await make_session(db, user_id, status="awaiting_answer")
    turn_a = await db.fetchval(
        """
        INSERT INTO turns (session_id, ordinal, kind, question_type, main_index, topic,
                           question_text, rubric, catalog_entry_id)
        VALUES ($1, 1, 'main', 'hr', 1, 'self_introduction', 'Вопрос', '{}'::jsonb, 'x')
        RETURNING id
        """,
        session_a,
    )
    # завершаем первую сессию, чтобы создать вторую активную
    await db.execute(
        "UPDATE sessions SET status='completed', finished_at=now() WHERE id=$1", session_a
    )
    session_b = await make_session(db, user_id, status="awaiting_answer")
    # отложенный составной FK проверяется на границе транзакции оператора
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute(
            """
            INSERT INTO jobs (unique_key, session_id, turn_id, kind, expected_version, payload)
            VALUES ('assess:test', $1, $2, 'assess', 1, '{}'::jsonb)
            """,
            session_b,
            turn_a,
        )


async def test_jobs_turn_required_for_assess(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO jobs (unique_key, session_id, kind, expected_version, payload)
            VALUES ('assess:no-turn', $1, 'assess', 1, '{}'::jsonb)
            """,
            session_id,
        )
    await db.execute(
        """
        INSERT INTO jobs (unique_key, session_id, kind, expected_version, payload)
        VALUES ('report:1', $1, 'report', 1, '{}'::jsonb)
        """,
        session_id,
    )
    # unique_key уникален — идемпотентное создание задания (§2.2)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO jobs (unique_key, session_id, kind, expected_version, payload)
            VALUES ('report:1', $1, 'report', 1, '{}'::jsonb)
            """,
            session_id,
        )


async def test_outbox_order_and_receipts(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id)
    await db.execute(
        """
        INSERT INTO outbox (unique_key, user_id, session_id, chat_id, kind, seq, payload)
        VALUES ('q:1', $1, $2, 555, 'question_text', 1, '{}'::jsonb)
        """,
        user_id,
        session_id,
    )
    # UNIQUE(chat_id, seq) — порядок outbox
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO outbox (unique_key, user_id, session_id, chat_id, kind, seq, payload)
            VALUES ('q:2', $1, $2, 555, 'question_text', 1, '{}'::jsonb)
            """,
            user_id,
            session_id,
        )
    # delivered без message_id запрещён
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO outbox (unique_key, user_id, session_id, chat_id, kind, seq,
                                state, payload)
            VALUES ('q:3', $1, $2, 555, 'text', 2, 'delivered', '{}'::jsonb)
            """,
            user_id,
            session_id,
        )


async def test_reports_constraints(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id, status="reporting")
    await db.execute(
        """
        INSERT INTO reports (session_id, report_json, parts, partial, total_chars)
        VALUES ($1, '{}'::jsonb, '["часть 1"]'::jsonb, true, 100)
        """,
        session_id,
    )
    # один отчёт на сессию (§2.3)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO reports (session_id, report_json, parts, partial, total_chars)
            VALUES ($1, '{}'::jsonb, '["x"]'::jsonb, true, 10)
            """,
            session_id,
        )
    # delivered_at требует delivery_started_at
    session2 = await make_session(db, user_id, status="completed")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO reports (session_id, report_json, parts, partial, total_chars,
                                 delivered_at)
            VALUES ($1, '{}'::jsonb, '["x"]'::jsonb, true, 10, now())
            """,
            session2,
        )
    # parts: 1..4 сообщения (§1.3)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO reports (session_id, report_json, parts, partial, total_chars)
            VALUES ($1, '{}'::jsonb, '["a","b","c","d","e"]'::jsonb, true, 10)
            """,
            session2,
        )


async def test_events_dedup_and_names(db: asyncpg.Connection) -> None:
    await db.execute(
        "INSERT INTO events (dedup_key, name) VALUES ('k1', 'start')"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO events (dedup_key, name) VALUES ('k1', 'start')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO events (dedup_key, name) VALUES ('k2', 'answer_text')")


async def test_ai_calls_unique_and_cost_rules(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    session_id = await make_session(db, user_id)
    turn_id = await db.fetchval(
        """
        INSERT INTO turns (session_id, ordinal, kind, question_type, main_index, topic,
                           question_text, rubric, catalog_entry_id)
        VALUES ($1, 1, 'main', 'hr', 1, 'self_introduction', 'В?', '{}'::jsonb, 'x')
        RETURNING id
        """,
        session_id,
    )
    job_id = await db.fetchval(
        """
        INSERT INTO jobs (unique_key, session_id, turn_id, kind, expected_version, payload)
        VALUES ('assess:j1', $1, $2, 'assess', 1, '{}'::jsonb) RETURNING id
        """,
        session_id,
        turn_id,
    )
    await db.execute(
        """
        INSERT INTO ai_calls (job_id, attempt, operation, provider, model, profile_version,
                              reserved_usd, result_state)
        VALUES ($1, 1, 'assess', 'test', 'test-model', '2.0', 0.01, 'unknown')
        """,
        job_id,
    )
    # UNIQUE(job_id, attempt)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO ai_calls (job_id, attempt, operation, provider, model,
                                  profile_version, reserved_usd)
            VALUES ($1, 1, 'assess', 'test', 'test-model', '2.0', 0.01)
            """,
            job_id,
        )
    # успех требует фактическую стоимость
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO ai_calls (job_id, attempt, operation, provider, model,
                                  profile_version, reserved_usd, result_state)
            VALUES ($1, 2, 'assess', 'test', 'test-model', '2.0', 0.01, 'success')
            """,
            job_id,
        )
    # неизвестный результат: actual NULL — остаётся в расходах до сверки (§3.2)
    row = await db.fetchrow(
        "SELECT actual_usd FROM ai_calls WHERE job_id = $1 AND attempt = 1", job_id
    )
    assert row["actual_usd"] is None


async def test_ui_tokens_and_tombstones(db: asyncpg.Connection) -> None:
    user_id = await make_user(db)
    await db.execute(
        """
        INSERT INTO ui_tokens (token, user_id, action, expires_at)
        VALUES ('abcdefghij0123456789ab', $1, 'select_role', now() + interval '30 min')
        """,
        user_id,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO ui_tokens (token, user_id, action, expires_at)
            VALUES ('abcdefghij0123456789ac', $1, 'unknown_action', now())
            """,
            user_id,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            """
            INSERT INTO deletion_tombstones (user_id, deleted_at, expires_at)
            VALUES ($1, now(), now() - interval '1 day')
            """,
            user_id,
        )
    await db.execute(
        """
        INSERT INTO deletion_tombstones (user_id, deleted_at, expires_at)
        VALUES ($1, now(), now() + interval '7 days')
        """,
        user_id,
    )


async def test_current_turn_composite_fk(db: asyncpg.Connection) -> None:
    """current_turn_id имеет ту же составную проверку (§2.3): turn чужой
    сессии привязать нельзя."""
    user_id = await make_user(db)
    session_a = await make_session(db, user_id, status="awaiting_answer")
    turn_a = await db.fetchval(
        """
        INSERT INTO turns (session_id, ordinal, kind, question_type, main_index, topic,
                           question_text, rubric, catalog_entry_id)
        VALUES ($1, 1, 'main', 'hr', 1, 'self_introduction', 'В?', '{}'::jsonb, 'x')
        RETURNING id
        """,
        session_a,
    )
    await db.execute(
        "UPDATE sessions SET status='completed', finished_at=now() WHERE id=$1", session_a
    )
    session_b = await make_session(db, user_id, status="preparing")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with db.transaction():
            await db.execute(
                "UPDATE sessions SET current_turn_id = $1 WHERE id = $2",
                turn_a,
                session_b,
            )
            # в своей сессии — корректно (проверка отложенная, форсируем)
    await db.execute(
        """
        INSERT INTO turns (session_id, ordinal, kind, question_type, main_index, topic,
                           question_text, rubric, catalog_entry_id)
        VALUES ($1, 1, 'main', 'hr', 1, 'self_introduction', 'В?', '{}'::jsonb, 'x')
        """,
        session_b,
    )
    turn_b = await db.fetchval(
        "SELECT id FROM turns WHERE session_id = $1", session_b
    )
    await db.execute(
        "UPDATE sessions SET current_turn_id = $1 WHERE id = $2", turn_b, session_b
    )
