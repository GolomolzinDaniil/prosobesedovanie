"""Приём ответов, пропуски, отмена/завершение, доставка outbox (§2.2, §2.4,
§3.2, §3.3): граница части 2 — ответ сохраняется, assess-задание создаётся и
ожидает AI-обработчик части 3.
"""

from __future__ import annotations

from datetime import timedelta

from app.telegram import messages
from app.telegram.bot import SendOutcome, SendResult


async def start_and_get_turn(harness):
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turns = await harness.fetch_turns(session["id"])
    return session, turns[0]


# ---------------------------------------------------------------------------
# Приём текстового ответа (§2.2, §3.3)
# ---------------------------------------------------------------------------


async def test_answer_accepted_atomically(harness) -> None:
    session, turn = await start_and_get_turn(harness)
    await harness.send_text(
        "Учусь на третьем курсе, пишу пет-проект.\r\nЛюблю Python за читаемость.",
        reply_to_message_id=turn["delivered_message_id"],
    )

    turns = await harness.fetch_turns(session["id"])
    saved = turns[0]
    # нормализация: CRLF -> LF, без внешних пробелов (§3.3)
    assert saved["answer_text"] == (
        "Учусь на третьем курсе, пишу пет-проект.\nЛюблю Python за читаемость."
    )
    assert saved["answer_received_at"] is not None

    updated = await harness.fetch_active_session()
    assert updated["status"] == "evaluating"
    # версия увеличилась при изменении управления сессией (§2.4)
    assert updated["version"] > session["version"]

    # assess-задание создано в той же транзакции, уникально, с ожидаемой версией
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job is not None
    assert job["kind"] == "assess" and job["state"] == "pending"
    assert job["expected_version"] == updated["version"]

    # подтверждение «Ответ принят. Готовлю следующий вопрос.» — reply на ответ
    ack = await harness.fetch_outbox(f"ack:{turn['id']}")
    assert ack is not None and ack["state"] == "delivered"
    assert any(messages.ANSWER_ACCEPTED in t for t in harness.bot.texts())

    # события answer_accepted и first_answer с дедупликацией
    async with harness.pool.acquire() as conn:
        events = await conn.fetch("SELECT name FROM events")
    names = [e["name"] for e in events]
    assert "answer_accepted" in names and "first_answer" in names


async def test_answer_requires_reply_to_current_question(harness) -> None:
    session, turn = await start_and_get_turn(harness)
    # без reply
    await harness.send_text("Ответ без reply")
    assert any(messages.REPLY_REQUIRED_HINT in t for t in harness.bot.texts())
    # reply на постороннее сообщение
    await harness.send_text("Ответ не туда", reply_to_message_id=turn["delivered_message_id"] + 500)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["answer_text"] is None
    assert (await harness.fetch_active_session())["status"] == "awaiting_answer"
    assert await harness.fetch_jobs(f"assess:{turn['id']}") is None


async def test_second_answer_returns_busy_without_new_job(harness) -> None:
    """Двойной ответ одному turn_id после первого принятия -> BUSY,
    без нового задания и без перезаписи ответа (§2.4, AT05)."""
    session, turn = await start_and_get_turn(harness)
    first = "Первый ответ"
    await harness.send_text(first, reply_to_message_id=turn["delivered_message_id"])
    await harness.send_text("Второй ответ!", reply_to_message_id=turn["delivered_message_id"])
    assert any(messages.BUSY_WAIT_MSG in t for t in harness.bot.texts())
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["answer_text"] == first  # исходный ответ не изменён
    async with harness.pool.acquire() as conn:
        jobs = await conn.fetch("SELECT * FROM jobs WHERE turn_id = $1", turn["id"])
    assert len(jobs) == 1


async def test_empty_and_overlong_answers_rejected(harness) -> None:
    """AT09-частично: границы 0 и 4001; ответ не принимается, вопрос сохранён."""
    session, turn = await start_and_get_turn(harness)
    await harness.send_text("   \n  ", reply_to_message_id=turn["delivered_message_id"])
    assert any(
        "Напишите ответ от 1 до 4000 символов" in t for t in harness.bot.texts()
    )
    await harness.send_text(
        "а" * 4001, reply_to_message_id=turn["delivered_message_id"]
    )
    assert any(
        "Ответ должен содержать не более 4000 символов." in t for t in harness.bot.texts()
    )
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["answer_text"] is None
    assert (await harness.fetch_active_session())["status"] == "awaiting_answer"


async def test_boundary_4000_chars_accepted(harness) -> None:
    session, turn = await start_and_get_turn(harness)
    await harness.send_text("а" * 4000, reply_to_message_id=turn["delivered_message_id"])
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["answer_text"] is not None
    assert len(turns[0]["answer_text"]) == 4000


async def test_edited_like_repeat_does_not_overwrite_answer(harness) -> None:
    """Повтор того же update не принимает ответ второй раз (§2.4)."""
    _session, turn = await start_and_get_turn(harness)
    update_id = harness.next_update_id()
    await harness.send_text(
        "Ответ", reply_to_message_id=turn["delivered_message_id"], update_id=update_id
    )
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE inbox SET status='pending', processed_at=NULL WHERE update_id=$1",
            update_id,
        )
    await harness.process_inbox()
    await harness.drain_outbox()
    async with harness.pool.acquire() as conn:
        jobs = await conn.fetch("SELECT * FROM jobs WHERE turn_id = $1", turn["id"])
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Пропуск и движение по плану (§3.3)
# ---------------------------------------------------------------------------


async def test_skip_advances_to_next_main_topic(harness) -> None:
    session, turn1 = await start_and_get_turn(harness)
    await harness.click(await harness.button_for("skip_turn", str(turn1["id"])))

    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["skipped"] is True and turns[0]["answer_text"] is None
    assert len(turns) == 2
    assert turns[1]["main_index"] == 2 and turns[1]["topic"] == "project_story"
    assert turns[1]["delivered_message_id"] is not None
    updated = await harness.fetch_active_session()
    assert updated["status"] == "awaiting_answer"
    assert updated["current_turn_id"] == turns[1]["id"]
    assert any(messages.SKIPPED_QUESTION in t for t in harness.bot.texts())


async def test_skip_all_six_cancels_and_returns_credit(harness) -> None:
    """Все шесть пропущенных вопросов без принятого ответа -> cancelled и
    освобождение попытки (§3.3), попытка сохранена (§3.7)."""
    session, _ = await start_and_get_turn(harness)
    for _ in range(6):
        current = await harness.fetch_active_session()
        turns = await harness.fetch_turns(current["id"])
        last = turns[-1]
        await harness.click(await harness.button_for("skip_turn", str(last["id"])))

    final = await harness.fetch_active_session()
    assert final is None
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session["id"])
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"
    assert any(messages.ALL_SKIPPED in t for t in harness.bot.texts())
    # отчёта нет
    async with harness.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM reports") == 0


async def test_skip_stale_turn_button(harness) -> None:
    session, turn1 = await start_and_get_turn(harness)
    old_button = await harness.button_for("skip_turn", str(turn1["id"]))
    await harness.click(old_button)  # пропускаем вопрос 1
    # повторный клик по кнопке прежнего вопроса -> STALE, новый пропуск не создаётся
    await harness.click(old_button)
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())
    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 2
    assert turns[1]["skipped"] is False


# ---------------------------------------------------------------------------
# Завершение и отмена (§2.4, §3.1)
# ---------------------------------------------------------------------------


async def test_finish_without_answers_cancels(harness) -> None:
    """NO_ANSWERS: завершение без единого принятого ответа -> cancelled,
    резерв освобождён (§3.3)."""
    session, _turn = await start_and_get_turn(harness)
    await harness.click(await harness.button_for("finish_session"))
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, finished_at FROM sessions WHERE id=$1", session["id"]
        )
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"
    assert any(messages.ALL_SKIPPED in t for t in harness.bot.texts())


async def test_finish_during_evaluation_records_requested_action(harness) -> None:
    _session, turn = await start_and_get_turn(harness)
    await harness.send_text("Мой ответ", reply_to_message_id=turn["delivered_message_id"])
    assert (await harness.fetch_active_session())["status"] == "evaluating"
    await harness.click(await harness.button_for("finish_session"))
    updated = await harness.fetch_active_session()
    assert updated["status"] == "evaluating"
    assert updated["requested_action"] == "finish"
    assert any(messages.FINISH_RECORDED in t for t in harness.bot.texts())


async def test_cancel_requires_confirmation_and_releases_credit(harness) -> None:
    _session, _ = await start_and_get_turn(harness)
    await harness.click(await harness.button_for("cancel_session"))
    # показано подтверждение, сессия ещё жива
    assert any(messages.CANCEL_CONFIRM in t for t in harness.bot.texts())
    assert await harness.fetch_active_session() is not None

    confirm_data = await harness.button_for("cancel_confirm")
    await harness.click(confirm_data)
    assert await harness.fetch_active_session() is None
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"
    assert any(messages.CANCELLED in t for t in harness.bot.texts())

    # терминальное состояние не возобновляется повторным кликом (§2.4)
    await harness.click(confirm_data)
    assert await harness.fetch_active_session() is None
    async with harness.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE status NOT IN "
            "('completed','cancelled','expired','failed')"
        )
    assert count == 0


async def test_cancel_decline_keeps_session(harness) -> None:
    _session, turn = await start_and_get_turn(harness)
    await harness.click(await harness.button_for("cancel_session"))
    await harness.click(await harness.button_for("cancel_decline"))
    assert any(messages.CONTROL_RERENDER in t for t in harness.bot.texts())
    assert await harness.fetch_active_session() is not None
    # управление вопросом доступно снова
    assert await harness.button_for("skip_turn", str(turn["id"]))


# ---------------------------------------------------------------------------
# Sender: порядок, повторы, 429, отказы (§1.4, §3.2)
# ---------------------------------------------------------------------------


async def test_question_and_controls_order_and_force_reply(harness) -> None:
    await harness.complete_entry()
    kinds = [
        (p.get("reply_markup") or {}).get("force_reply") is True for p in harness.bot.sent
    ]
    assert any(kinds), "вопрос обязан отправляться с ForceReply"


async def test_required_order_blocks_next_until_confirmed(harness) -> None:
    """Следующий обязательный question_text не отправляется, пока предыдущий
    не подтверждён (§2.4)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()

    # ставим в очередь два «вопроса» и роняем первый один раз
    async with harness.pool.acquire() as conn:
        from app.storage import outbox_repo
        from app.telegram.markup import text_payload

        user = await conn.fetchrow("SELECT * FROM users")
        await outbox_repo.enqueue(
            conn,
            unique_key="test:order-q1",
            user_id=user["id"],
            chat_id=user["chat_id"],
            kind="question_text",
            payload=text_payload(user["chat_id"], "Порядок-1"),
            session_id=session["id"],
        )
        await outbox_repo.enqueue(
            conn,
            unique_key="test:order-q2",
            user_id=user["id"],
            chat_id=user["chat_id"],
            kind="question_text",
            payload=text_payload(user["chat_id"], "Порядок-2"),
            session_id=session["id"],
        )
    harness.bot.set_failure_rule(
        lambda p: p.get("text") == "Порядок-1",
        SendResult(SendOutcome.retryable, error="net"),
        times=1,
    )
    await harness.drain_outbox()
    texts = harness.bot.texts()
    assert "Порядок-1" in texts
    assert "Порядок-2" not in texts  # заблокирован неподтверждённым первым

    # после успешного повтора первого второй уходит
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE outbox SET next_attempt_at = NULL WHERE unique_key = 'test:order-q1'"
        )
    await harness.drain_outbox()
    texts = harness.bot.texts()
    assert texts.index("Порядок-1") < texts.index("Порядок-2")
    row2 = await harness.fetch_outbox("test:order-q2")
    assert row2["state"] == "delivered"


async def test_retry_after_respected(harness) -> None:
    await harness.send_text("/start")
    data = await harness.button_for("consent_accept")
    harness.bot.script.clear()
    harness.bot.queue_result(
        SendResult(SendOutcome.retry_after, retry_after_seconds=7, error="429")
    )
    await harness.click(data)
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT next_attempt_at, state, attempts FROM outbox
            WHERE chat_id = 555 AND state = 'pending' ORDER BY created_at LIMIT 1
            """
        )
        delta = row["next_attempt_at"] - await conn.fetchval("SELECT now()")
    assert row["state"] == "pending"
    assert timedelta(seconds=4) < delta <= timedelta(seconds=8)
    # после окна повтор доставляется
    async with harness.pool.acquire() as conn:
        await conn.execute("UPDATE outbox SET next_attempt_at = now()")
    await harness.drain_outbox()
    async with harness.pool.acquire() as conn:
        delivered = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE chat_id=555 AND state='delivered'"
        )
    assert delivered >= 1


async def test_terminal_failure_fails_session_and_releases_credit(harness) -> None:
    """Исчерпание повторов доставки вопроса: failed + резерв освобождён +
    уведомление (§2.4, §3.2 DELIVERY_FAILED)."""
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    await harness.click(await harness.button_for("select_role", "python"))
    await harness.click(await harness.button_for("select_level", "python:junior"))
    await harness.click(await harness.button_for("select_format", "python:junior:text"))

    # вопрос отказывает сетью 10 раз (лимит повторов sender-а)
    harness.bot.set_failure_rule(
        lambda p: "Основной вопрос" in (p.get("text") or ""),
        SendResult(SendOutcome.retryable, error="net"),
        times=10,
    )
    await harness.click(await harness.button_for("start_session", "python:junior:text"))
    for _ in range(15):
        async with harness.pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET next_attempt_at = NULL WHERE state = 'pending'"
            )
        if await harness.drain_outbox() == 0:
            break

    async with harness.pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT status, finished_at FROM sessions ORDER BY created_at DESC LIMIT 1"
        )
        credit = await conn.fetchrow("SELECT state FROM credits")
    assert session["status"] == "failed" and session["finished_at"] is not None
    assert credit["state"] == "available"
    assert any(messages.QUESTION_DELIVERY_FAILED in t for t in harness.bot.texts())


async def test_chat_unreachable_no_notification(harness) -> None:
    """Telegram 403: чат недоступен — сессия failed, уведомление не
    отправляется (доставить некому), бесконечных повторов нет (§3.2)."""
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    await harness.click(await harness.button_for("select_role", "python"))
    await harness.click(await harness.button_for("select_level", "python:junior"))
    await harness.click(await harness.button_for("select_format", "python:junior:text"))
    harness.bot.set_failure_rule(
        lambda p: "Основной вопрос" in (p.get("text") or ""),
        SendResult(SendOutcome.chat_unreachable, error="403"),
    )
    await harness.click(await harness.button_for("start_session", "python:junior:text"))
    for _ in range(5):
        async with harness.pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET next_attempt_at = NULL WHERE state = 'pending'"
            )
        if await harness.drain_outbox() == 0:
            break
    async with harness.pool.acquire() as conn:
        session = await conn.fetchrow("SELECT status FROM sessions")
        credit = await conn.fetchrow("SELECT state FROM credits")
        failed_rows = await conn.fetch(
            "SELECT attempts FROM outbox WHERE state = 'failed'"
        )
    assert session["status"] == "failed"
    assert credit["state"] == "available"
    assert all(r["attempts"] == 1 for r in failed_rows)  # 403 не повторяется
    assert not any(messages.QUESTION_DELIVERY_FAILED in t for t in harness.bot.texts())


async def test_bad_request_is_terminal(harness) -> None:
    """400 с некорректным payload — ошибка реализации, не повторяется (§3.2)."""
    await harness.send_text("/start")
    harness.bot.script.clear()
    harness.bot.queue_result(SendResult(SendOutcome.bad_request, error="400"))
    await harness.click(await harness.button_for("consent_accept"))
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, attempts, last_error FROM outbox WHERE state='failed' LIMIT 1"
        )
    assert row is not None and "400" in row["last_error"]
    assert row["attempts"] == 1  # одна попытка, бесконечных повторов нет


async def test_cancelled_session_question_not_sent(harness) -> None:
    """При восстановлении/гонке результат отменённой сессии не отправляется
    (§3.2): строки отменённой сессии снимаются с доставки."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    # ставим новую строку в очередь и отменяем сессию до её доставки
    async with harness.pool.acquire() as conn:
        from app.storage import outbox_repo
        from app.telegram.markup import text_payload

        user = await conn.fetchrow("SELECT * FROM users")
        await outbox_repo.enqueue(
            conn,
            unique_key="test:pending-msg",
            user_id=user["id"],
            chat_id=user["chat_id"],
            kind="question_text",
            payload=text_payload(user["chat_id"], "Это не должно уйти"),
            session_id=session["id"],
        )
        await conn.execute(
            """
            UPDATE sessions SET status='cancelled', finished_at=now(), version=version+1
            WHERE id=$1
            """,
            session["id"],
        )
    sent_before = len(harness.bot.sent)
    await harness.drain_outbox()
    assert len(harness.bot.sent) == sent_before
    row = await harness.fetch_outbox("test:pending-msg")
    assert row["state"] == "cancelled"
