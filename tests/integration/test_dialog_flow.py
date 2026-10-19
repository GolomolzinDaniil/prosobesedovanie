"""Поток входа и управления (§3.7, AT01-частично, AT16-частично):
/start -> согласие -> выбор -> сводка -> старт; команды; группы; удаление;
устаревшие кнопки; лимиты стартов и команд; идемпотентность повторной
обработки update.
"""

from __future__ import annotations

import json

from app.telegram import messages

# ---------------------------------------------------------------------------
# Вход, согласие и выбор параметров (T07)
# ---------------------------------------------------------------------------


async def test_start_renders_greeting_and_consent(harness) -> None:
    await harness.send_text("/start")
    texts = harness.bot.texts()
    assert messages.GREETING in texts
    consent_notice = next(t for t in texts if "Уведомление об обработке" in t)
    assert "2.0" in consent_notice  # версия документа
    buttons = harness.bot.find_button("consent_accept")
    assert buttons and buttons.startswith("consent_accept:")
    user = await harness.fetch_user()
    assert user is not None
    assert user["consent_version"] is None
    assert user["ui_state"] == "awaiting_consent"


async def test_consent_records_version_date_and_trial_credit(harness) -> None:
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    user = await harness.fetch_user()
    assert user["consent_version"] == messages.CONSENT_DOCUMENT_VERSION
    assert user["consent_at"] is not None
    credits = await harness.fetch_credits()
    assert len(credits) == 1
    assert credits[0]["source"] == "trial"
    assert credits[0]["state"] == "available"
    assert messages.SELECT_ROLE in harness.bot.texts()


async def test_consent_decline_blocks_interview_keeps_help(harness) -> None:
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_decline"))
    assert any(messages.CONSENT_DECLINED in t for t in harness.bot.texts())
    user = await harness.fetch_user()
    assert user["consent_version"] is None
    assert user["ui_state"] == "consent_declined"
    # справка остаётся доступной
    await harness.send_text("/help")
    assert any(messages.HELP_TEXT in t for t in harness.bot.texts())
    # интервью заблокировано: /start снова показывает согласие
    await harness.send_text("/start")
    assert await harness.button_for("consent_accept")


async def test_selection_navigation_and_back(harness) -> None:
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    assert messages.SELECT_ROLE in harness.bot.last_text()

    await harness.click(await harness.button_for("select_role", "analyst"))
    assert messages.SELECT_LEVEL in harness.bot.last_text()

    # «Назад» к роли
    await harness.click(await harness.button_for("back_to_role"))
    assert messages.SELECT_ROLE in harness.bot.last_text()

    await harness.click(await harness.button_for("select_role", "qa"))
    await harness.click(await harness.button_for("select_level", "qa:intern"))
    assert messages.SELECT_FORMAT in harness.bot.last_text()
    await harness.click(await harness.button_for("back_to_level", "qa"))
    assert messages.SELECT_LEVEL in harness.bot.last_text()

    await harness.click(await harness.button_for("select_level", "qa:intern"))
    await harness.click(await harness.button_for("select_format", "qa:intern:voice"))
    summary = harness.bot.last_text()
    assert "QA-инженер" in summary and "Стажёр" in summary and "Голос" in summary
    assert "резервирует попытку" in summary  # «Начать» расходует резерв, не попытку


async def test_full_start_creates_session_and_delivers_first_question(harness) -> None:
    await harness.complete_entry()  # python/junior/text

    session = await harness.fetch_active_session()
    assert session is not None
    assert session["role_id"] == "python"
    assert session["level"] == "junior"
    assert session["format"] == "text"
    # после подтверждения доставки вопрос -> awaiting_answer (§2.2)
    assert session["status"] == "awaiting_answer"
    assert session["catalog_version"] == "2.0"
    assert session["prompt_version"] == "2.0"
    assert session["current_difficulty"] == 2  # junior стартует со сложности 2
    assert session["current_turn_id"] is not None

    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 1
    turn = turns[0]
    assert turn["kind"] == "main" and turn["main_index"] == 1
    assert turn["question_type"] == "hr" and turn["topic"] == "self_introduction"
    assert turn["catalog_entry_id"] == "python-junior-hr-self-introduction"
    assert turn["delivered_message_id"] is not None

    # кредит зарезервирован за сессией
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "reserved"
    assert credits[0]["reserved_session_id"] == session["id"]

    # сообщения: сводка старта, вопрос c ForceReply, управление — отдельным
    sent = harness.bot.sent
    assert any("Тренировка начата" in (p.get("text") or "") for p in sent)
    question_idx = next(
        i for i, p in enumerate(sent) if "Основной вопрос 1 из 6" in (p.get("text") or "")
    )
    assert harness.bot.has_force_reply(question_idx)
    control = sent[question_idx + 1]
    assert control["text"] == messages.CONTROL_MESSAGE
    keyboard = control["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in keyboard for b in row]
    assert labels == [messages.SKIP_LABEL, messages.FINISH_LABEL, messages.CANCEL_LABEL]
    # ForceReply и InlineKeyboardMarkup не в одном сообщении [S1]
    assert "inline_keyboard" not in (sent[question_idx].get("reply_markup") or {})

    # событие start зарегистрировано с дедупликацией по бизнес-переходу
    async with harness.pool.acquire() as conn:
        events = await conn.fetch("SELECT name, dedup_key FROM events")
    assert any(e["name"] == "start" and e["dedup_key"] == f"start:{session['id']}" for e in events)


async def test_all_six_role_level_combinations_start(harness) -> None:
    """AT01 (частично): все 6 комбинаций роли/уровня создают сессию с первым
    вопросом правильной темы; формат text (voice-вопросы — часть 4)."""
    expected_first_entry = {
        ("python", "intern"): "python-intern-hr-self-introduction",
        ("python", "junior"): "python-junior-hr-self-introduction",
        ("analyst", "intern"): "analyst-intern-hr-self-introduction",
        ("analyst", "junior"): "analyst-junior-hr-self-introduction",
        ("qa", "intern"): "qa-intern-hr-self-introduction",
        ("qa", "junior"): "qa-junior-hr-self-introduction",
    }
    chat_id = 555
    for index, ((role, level), entry_id) in enumerate(expected_first_entry.items()):
        if index > 0:
            # новая тренировка: завершаем предыдущую сессию отменой
            await harness.cancel_active(chat_id)
        # лимит 3 старта/24ч обнуляем для чистоты сценария AT01
        async with harness.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET created_at = created_at - interval '25 hours'"
            )
        await harness.complete_entry(chat_id, role=role, level=level, fmt="text")
        session = await harness.fetch_active_session(chat_id)
        assert session is not None, (role, level)
        assert session["role_id"] == role and session["level"] == level
        turns = await harness.fetch_turns(session["id"])
        assert turns[0]["catalog_entry_id"] == entry_id
        # первый вопрос — самопрезентация правильной темы
        assert turns[0]["topic"] == "self_introduction"
        assert turns[0]["question_text"]


async def test_start_without_consent_shows_consent_required(harness) -> None:
    await harness.send_text("/start")
    user = await harness.fetch_user()
    # имитируем клик «Начать» без согласия: токен выдаём вручную
    async with harness.pool.acquire() as conn:
        from datetime import timedelta

        from app.services.ui_tokens import generate_ui_token
        from app.storage import ui_tokens_repo

        token = generate_ui_token()
        await ui_tokens_repo.issue(
            conn,
            token=token,
            user_id=user["id"],
            action="start_session",
            ttl=timedelta(minutes=30),
            allowed_value="python:junior:text",
        )
        await harness.click(f"start_session:{token}")
    assert any("Подтвердите условия обработки данных" in t for t in harness.bot.texts())
    # сессия не создана (§3.1: предусловие «согласие»)
    assert await harness.fetch_active_session() is None


# ---------------------------------------------------------------------------
# Устаревшие и чужие кнопки (§2.4, §3.7)
# ---------------------------------------------------------------------------


async def test_token_single_use_second_click_stale(harness) -> None:
    await harness.send_text("/start")
    data = await harness.button_for("consent_accept")
    await harness.click(data)
    before = len(harness.bot.sent)
    await harness.click(data)  # повторное использование
    texts = harness.bot.texts()
    assert any(messages.STALE_BUTTON in t for t in texts[before:])
    user = await harness.fetch_user()
    assert user["consent_version"] is not None  # эффект не повторился дважды


async def test_expired_token_shows_actual_screen(harness) -> None:
    await harness.send_text("/start")
    data = await harness.button_for("consent_accept")
    token = data.split(":", 1)[1]
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE ui_tokens SET expires_at = now() - interval '1 minute' WHERE token = $1",
            token,
        )
    await harness.click(data)
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())
    # показан актуальный экран согласия (§3.1)
    assert await harness.button_for("consent_accept")


async def test_foreign_owner_click_ignored(harness) -> None:
    await harness.send_text("/start")
    data = await harness.button_for("consent_accept")
    # клик из другого аккаунта: владелец не совпадает -> токен не потребляется
    await harness.click(data, chat_id=999)
    user = await harness.fetch_user()
    assert user["consent_version"] is None
    # у чужого пользователя данные не раскрываются: профиль не создан
    assert await harness.fetch_user(999) is None


async def test_old_selection_button_after_navigation(harness) -> None:
    """Старые кнопки экрана не управляют новым состоянием (§2.4)."""
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    old_role_button = await harness.button_for("select_role", "python")
    await harness.click(await harness.button_for("select_role", "analyst"))
    # прежняя кнопка роли отозвана при перерисовке экрана
    await harness.click(old_role_button)
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())
    # пользователь остался на экране уровня (аналитик)
    assert messages.SELECT_LEVEL in harness.bot.last_text() or any(
        messages.SELECT_LEVEL in t for t in harness.bot.texts()[-2:]
    )


# ---------------------------------------------------------------------------
# Команды и пустые состояния (§3.7)
# ---------------------------------------------------------------------------


async def test_help_available_anytime(harness) -> None:
    await harness.send_text("/help")
    assert any(messages.HELP_TEXT in t for t in harness.bot.texts())
    # профиль не создаётся до /start: справка без сбора данных
    assert await harness.fetch_user() is None


async def test_status_empty_and_active(harness) -> None:
    await harness.send_text("/start")
    await harness.send_text("/status")
    assert any(messages.NO_ACTIVE_SESSION in t for t in harness.bot.texts())

    await harness.complete_entry()
    await harness.send_text("/status")
    last = harness.bot.last_text()
    assert "Активная тренировка" in last
    assert "Python-разработчик" in last and "Junior" in last
    assert messages.STATUS_AWAITING_ANSWER in last


async def test_report_empty_state(harness) -> None:
    await harness.send_text("/start")
    await harness.send_text("/report")
    assert any(messages.NO_REPORT_YET in t for t in harness.bot.texts())


async def test_balance_states(harness) -> None:
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    await harness.send_text("/balance")
    text = harness.bot.last_text()
    assert "Доступно: 1" in text and messages.BALANCE_TRIAL_AVAILABLE in text

    await harness.complete_entry()
    await harness.send_text("/balance")
    text = harness.bot.last_text()
    assert "Доступно: 0" in text and "Зарезервировано активной тренировкой: 1" in text


async def test_unknown_command_and_billing_disabled(harness) -> None:
    await harness.send_text("/start")
    await harness.send_text("/foobar")
    assert any(messages.UNKNOWN_COMMAND in t for t in harness.bot.texts())
    await harness.send_text("/buy")
    assert any(messages.BILLING_DISABLED in t for t in harness.bot.texts())


async def test_plain_text_without_session_guides(harness) -> None:
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    await harness.send_text("привет, а что делать?")
    assert any(messages.NO_ACTIVE_SESSION in t for t in harness.bot.texts())


# ---------------------------------------------------------------------------
# Группа и неподдерживаемое содержимое (§1.3)
# ---------------------------------------------------------------------------


async def test_group_start_suggests_private_chat(harness) -> None:
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 77,
            "date": 1760000000,
            "chat": {"id": -100500, "type": "group"},
            "from": {"id": 888},
            "text": "/start",
        },
    }
    await harness.push(update)
    assert any(messages.GROUP_CHAT_NOTICE in t for t in harness.bot.texts())
    # ответы в группе не сохраняются: профиль не создан
    assert await harness.fetch_user(888) is None


async def test_group_plain_text_ignored(harness) -> None:
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 78,
            "date": 1760000000,
            "chat": {"id": -100500, "type": "group"},
            "from": {"id": 888},
            "text": "обычное сообщение",
        },
    }
    await harness.push(update)
    assert harness.bot.sent == []


async def test_unsupported_content_in_private_chat(harness) -> None:
    await harness.send_text("/start")
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 79,
            "date": 1760000000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555},
            "photo": [{"file_id": "photo"}],
        },
    }
    await harness.push(update)
    # без сессии — подсказка; содержимое не обрабатывается
    assert await harness.fetch_user() is not None
    inbox_row = await harness.fetch_inbox(update["update_id"])
    assert inbox_row["status"] == "processed"


async def test_voice_in_text_session_rejected(harness) -> None:
    await harness.complete_entry(fmt="text")
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    assert any(messages.VOICE_IN_TEXT_FORMAT in t for t in harness.bot.texts())
    # ответ не сохранён
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["answer_text"] is None


async def test_voice_in_voice_session_starts_transcription(harness) -> None:
    """Часть 4: голос в voice-сессии запускает распознавание (§3.5):
    transcribing + transcribe-задание + счётчик попыток в БД."""
    await harness.complete_entry(fmt="voice")
    session = await harness.fetch_active_session()
    assert session["format"] == "voice"
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_voice(reply_to=turn["delivered_message_id"])

    session = await harness.fetch_active_session()
    assert session["status"] == "transcribing"
    turn = (await harness.fetch_turns(session["id"]))[0]
    assert turn["stt_attempts"] == 1
    jobs = await harness.fetch_jobs_by_kind("transcribe")
    assert len(jobs) == 1
    assert jobs[0]["unique_key"] == f"transcribe:{turn['id']}:1"
    assert any(messages.TRANSCRIBING in t for t in harness.bot.texts())


# ---------------------------------------------------------------------------
# Удаление профиля (§3.7; полный цикл T18 — часть 5)
# ---------------------------------------------------------------------------


async def test_delete_flow_blocks_new_actions(harness) -> None:
    await harness.complete_entry()
    await harness.send_text("/delete")
    assert any(messages.DELETE_WARNING in t for t in harness.bot.texts())
    await harness.click(await harness.button_for("confirm_delete"))

    user = await harness.fetch_user()
    assert user["deletion_requested_at"] is not None
    assert any(messages.DELETE_ACCEPTED in t for t in harness.bot.texts())

    # активная сессия отменена, резерв освобождён
    assert await harness.fetch_active_session() is None
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"

    # новые действия не принимаются
    await harness.send_text("/start")
    assert any(messages.DELETION_IN_PROGRESS in t for t in harness.bot.texts())
    assert await harness.fetch_active_session() is None


async def test_delete_declined(harness) -> None:
    await harness.send_text("/start")
    await harness.send_text("/delete")
    await harness.click(await harness.button_for("delete_decline"))
    assert any(messages.DELETE_DECLINED in t for t in harness.bot.texts())
    user = await harness.fetch_user()
    assert user["deletion_requested_at"] is None


# ---------------------------------------------------------------------------
# Лимиты (§1.3)
# ---------------------------------------------------------------------------


async def test_starts_per_24h_limit_counts_cancelled(harness) -> None:
    """3 старта за скользящие 24 часа; отменённый старт учитывается (§1.3)."""
    await harness.send_text("/start")
    await harness.click(await harness.button_for("consent_accept"))
    for _ in range(3):
        await harness.send_text("/start")
        await harness.click(await harness.button_for("select_role", "python"))
        await harness.click(await harness.button_for("select_level", "python:junior"))
        await harness.click(await harness.button_for("select_format", "python:junior:text"))
        await harness.click(await harness.button_for("start_session", "python:junior:text"))
        await harness.cancel_active()

    # каждый старт возвращает резерв, но лимит стартов исчерпан
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"
    await harness.send_text("/start")
    await harness.click(await harness.button_for("select_role", "python"))
    await harness.click(await harness.button_for("select_level", "python:junior"))
    await harness.click(await harness.button_for("select_format", "python:junior:text"))
    await harness.click(await harness.button_for("start_session", "python:junior:text"))
    assert any("3 тренировки" in t for t in harness.bot.texts())
    assert await harness.fetch_active_session() is None

    # сессии вне окна освобождают лимит
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET created_at = created_at - interval '25 hours'"
        )
    await harness.send_text("/start")
    await harness.click(await harness.button_for("select_role", "python"))
    await harness.click(await harness.button_for("select_level", "python:junior"))
    await harness.click(await harness.button_for("select_format", "python:junior:text"))
    await harness.click(await harness.button_for("start_session", "python:junior:text"))
    assert await harness.fetch_active_session() is not None


async def test_active_session_blocks_second_start(harness) -> None:
    await harness.complete_entry()
    await harness.send_text("/start")
    assert any(messages.ACTIVE_SESSION_HINT in t for t in harness.bot.texts())
    # вторая сессия не создана, резерв не списан второй раз
    async with harness.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM sessions")
        credits = await conn.fetch("SELECT state FROM credits")
    assert count == 1
    assert [c["state"] for c in credits] == ["reserved"]


async def test_no_credit_message_when_trial_used(harness) -> None:
    await harness.complete_entry()
    # имитируем израсходованную попытку (consumed после отчёта — часть 4)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE credits SET state = 'consumed', reserved_session_id = NULL,
                consumed_session_id = reserved_session_id
            """
        )
        await conn.execute(
            "UPDATE sessions SET status = 'completed', finished_at = now()"
        )
    await harness.send_text("/start")
    await harness.click(await harness.button_for("select_role", "python"))
    await harness.click(await harness.button_for("select_level", "python:junior"))
    await harness.click(await harness.button_for("select_format", "python:junior:text"))
    await harness.click(await harness.button_for("start_session", "python:junior:text"))
    assert any(messages.NO_CREDIT_BILLING_OFF in t for t in harness.bot.texts())
    assert await harness.fetch_active_session() is None


async def test_commands_rate_limit(harness, db_url) -> None:
    """Реальный лимит: 10 команд в минуту (§1.3)."""
    from tests.conftest import make_env
    from tests.fakes import Harness

    local = Harness(harness.pool, make_env(DATABASE_URL=db_url))  # реальные settings
    await local.send_text("/start")
    limited = False
    for _ in range(12):
        await local.send_text("/help")
        if any("Слишком много действий" in t for t in local.bot.texts()):
            limited = True
            break
    assert limited, "лимит 10 команд в минуту не сработал"
    # ограниченное действие не выполняется: профиль не получил новых эффектов
    row = await local.fetch_inbox(local._update_id)
    assert row["status"] == "processed"


# ---------------------------------------------------------------------------
# Идемпотентность и устойчивость (T06: повторы update не повторяют команду)
# ---------------------------------------------------------------------------


async def test_reprocessing_same_update_has_no_duplicate_effects(harness) -> None:
    """Повторная обработка того же update_id не повторяет доменных эффектов
    (T06): экраны идемпотентны по unique_key, привязанному к update_id."""
    update_id = harness.next_update_id()
    await harness.send_text("/start", update_id=update_id)
    sent_before = len(harness.bot.sent)

    # имитация: обновление возвращено в очередь и обработано повторно
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE inbox SET status = 'pending', processed_at = NULL WHERE update_id = $1",
            update_id,
        )
    await harness.process_inbox()
    await harness.drain_outbox()

    # новых сообщений нет
    assert len(harness.bot.sent) == sent_before
    async with harness.pool.acquire() as conn:
        users = await conn.fetch("SELECT * FROM users")
        tokens = await conn.fetch("SELECT * FROM ui_tokens WHERE consumed_at IS NULL")
    assert len(users) == 1
    # токены согласия не задублированы
    assert len([t for t in tokens if t["action"] == "consent_accept"]) == 1


async def test_handler_crash_retries_without_duplicating(harness, monkeypatch) -> None:
    """Сбой обработчика -> requeue; повтор не дублирует доменные эффекты."""
    import app.workers.inbox_worker as inbox_module

    await harness.send_text("/start")
    data = await harness.button_for("consent_accept")
    update_id = harness.next_update_id()

    calls = {"n": 0}
    original = inbox_module.handle_update

    async def flaky(conn, deps, item):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        await original(conn, deps, item)

    monkeypatch.setattr(inbox_module, "handle_update", flaky)

    from app.storage import inbox_repo as repo

    update = {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-crash",
            "from": {"id": 555},
            "chat_instance": "-1",
            "data": data,
        },
    }
    async with harness.pool.acquire() as conn:
        await repo.insert_update(
            conn, update_id=update_id, kind="callback_query", payload=update
        )
    await harness.process_inbox()
    row = await harness.fetch_inbox(update_id)
    assert row["status"] == "pending"  # requeue после сбоя
    assert row["attempts"] == 1

    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE inbox SET next_attempt_at = NULL WHERE update_id = $1", update_id
        )
    await harness.process_inbox()
    await harness.drain_outbox()
    row = await harness.fetch_inbox(update_id)
    assert row["status"] == "processed"
    user = await harness.fetch_user()
    assert user["consent_version"] is not None
    async with harness.pool.acquire() as conn:
        credits = await conn.fetch("SELECT * FROM credits")
    assert len(credits) == 1


async def test_campaign_code_only_from_allowlist(harness, db_url, tmp_path, repo_root) -> None:
    """Допустимый campaign-код сохраняется только из allowlist (§3.7)."""
    settings = json.loads(
        (repo_root / "config" / "settings.json").read_text(encoding="utf-8")
    )
    settings["campaign_source_allowlist"] = ["PROMO2026"]
    custom = tmp_path / "settings.json"
    custom.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")

    from tests.conftest import make_env
    from tests.fakes import Harness

    env = make_env(DATABASE_URL=db_url, SETTINGS_PATH=str(custom))
    local = Harness(harness.pool, env)

    await local.send_text("/start PROMO2026")
    user = await local.fetch_user()
    assert user["source_code"] == "PROMO2026"

    await local.send_text("/start HACKME", chat_id=777)
    user2 = await local.fetch_user(777)
    assert user2["source_code"] is None
