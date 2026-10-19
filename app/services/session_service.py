"""Доменные операции сессии (§2.2–2.4, §3.1, §3.3, §3.8; задачи T08, T09, T10).

Все функции выполняются внутри транзакции, начатой вызывающим кодом, при
уже заблокированной строке пользователя (порядок блокировок: users ->
sessions -> credits -> связанные строки, §2.3). Начало сессии и резерв прав
атомарны; ответ и assess-задание атомарны; отмена освобождает резерв.

Первый вопрос берётся из каталога без ожидания LLM (§2.2). AI-обработка
assess-заданий подключается в части 3; до этого задания остаются pending —
принятый ответ не теряется («сохранение вопроса, принятого ответа и задания
выполняется до подтверждения в чате»).
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from app.config import AppConfig, CatalogEntry, CatalogScenario
from app.domain.answers import validate_answer_text
from app.domain.errors import DomainError
from app.services.ui_tokens import generate_ui_token
from app.storage import (
    credits_repo,
    events_repo,
    jobs_repo,
    outbox_repo,
    sessions_repo,
    turns_repo,
    ui_tokens_repo,
    users_repo,
)
from app.telegram import messages
from app.telegram.markup import buttons_payload, force_reply_payload, reply_payload, text_payload
from contracts.models import (
    ErrorCode,
    InterviewFormat,
    Level,
    RoleId,
    SessionStatus,
    SessionView,
    TurnKind,
    TurnView,
)

TERMINAL = ("completed", "cancelled", "expired", "failed")


# ---------------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------------


def scenario_for(config: AppConfig, role: str, level: str) -> CatalogScenario:
    for scenario in config.catalog.scenarios:
        if scenario.role_id.value == role and scenario.level.value == level:
            return scenario
    # Неизвестная комбинация отклоняется перечислением (§1.3)
    raise DomainError(ErrorCode.INVALID_INPUT, field="role_id/level")


def entry_by_main_index(scenario: CatalogScenario, main_index: int) -> CatalogEntry:
    for entry in scenario.entries:
        if entry.main_index == main_index:
            return entry
    raise KeyError(f"main_index={main_index}")


# ---------------------------------------------------------------------------
# Вспомогательные построения
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _inactive_ttl(config: AppConfig) -> timedelta:
    return timedelta(hours=config.settings.retention.session_inactive_expire_hours)


def _question_control_ttl(config: AppConfig) -> timedelta:
    return timedelta(hours=config.settings.ui_tokens.question_control_hours)


def build_question_text(config: AppConfig, session: asyncpg.Record, turn: asyncpg.Record) -> str:
    header = messages.question_header(
        messages.ROLE_LABELS[session["role_id"]],
        messages.LEVEL_LABELS[session["level"]],
        turn["main_index"],
        config.settings.interview.main_questions,
    )
    hint = (
        messages.VOICE_QUESTION_REPLY_HINT
        if session["format"] == "voice"
        else messages.QUESTION_REPLY_HINT
    )
    return f"{header}\n\n{turn['question_text']}\n\n{hint}"


async def _enqueue(
    conn: asyncpg.Connection,
    *,
    unique_key: str,
    user: asyncpg.Record,
    payload: dict,
    kind: str = "text",
    session_id: uuid.UUID | None = None,
    is_required: bool = True,
) -> None:
    text = payload.get("text") or ""
    # Серверный предел части сообщения (§1.3): тексты формируются сервером
    if len(text) > 3500:
        raise AssertionError("внутренняя ошибка: текст сообщения длиннее предела части")
    await outbox_repo.enqueue(
        conn,
        unique_key=unique_key,
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind=kind,
        payload=payload,
        session_id=session_id,
        is_required=is_required,
    )


# ---------------------------------------------------------------------------
# Старт сессии (§2.2, §3.1, §3.8)
# ---------------------------------------------------------------------------


async def start_session_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    role: RoleId,
    level: Level,
    fmt: InterviewFormat,
    update_id: int,
) -> asyncpg.Record:
    """Резерв попытки + создание сессии + первый вопрос — атомарно."""
    if user["deletion_requested_at"] is not None:
        raise DomainError(ErrorCode.DELETION_IN_PROGRESS)
    if not user["consent_version"]:
        raise DomainError(ErrorCode.CONSENT_REQUIRED)

    active = await sessions_repo.find_active(conn, user["id"], for_update=True)
    if active is not None:
        raise DomainError(ErrorCode.ACTIVE_SESSION, message=messages.ACTIVE_SESSION_HINT)

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    starts = await sessions_repo.count_starts_since(conn, user["id"], window_start)
    limit = config.settings.limits.starts_per_24h
    if starts >= limit:
        oldest = await sessions_repo.oldest_start_since(conn, user["id"], window_start)
        retry_after = 60
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            retry_after = max(1, math.ceil((oldest + timedelta(hours=24) - now).total_seconds()))
        raise DomainError(
            ErrorCode.RATE_LIMITED,
            message=messages.STARTS_RATE_LIMITED.format(seconds=retry_after),
            retry_after_seconds=retry_after,
        )

    session_id = uuid.uuid4()
    profile_version = (
        config.provider_profile.version if config.ai_enabled and config.provider_profile else None
    )

    # Сессия создаётся до резерва: credits.reserved_session_id ссылается на неё.
    # При отсутствии доступной попытки NO_CREDIT откатывает всю транзакцию —
    # начало сессии и резерв прав атомарны (§2.3).
    await sessions_repo.create(
        conn,
        session_id=session_id,
        user_id=user["id"],
        role_id=role.value,
        level=level.value,
        fmt=fmt.value,
        catalog_version=config.catalog.version,
        prompt_version="2.0",
        model_profile_version=profile_version,
        current_difficulty=config.settings.interview.initial_difficulty[level],
        inactive_ttl=_inactive_ttl(config),
    )
    credit = await credits_repo.reserve_available(conn, user["id"], session_id)
    if credit is None:
        message = None
        if not config.settings.flags.billing_enabled and await credits_repo.trial_used(
            conn, user["id"]
        ):
            message = messages.NO_CREDIT_BILLING_OFF
        raise DomainError(ErrorCode.NO_CREDIT, message=message)

    scenario = scenario_for(config, role.value, level.value)
    first_entry = entry_by_main_index(scenario, 1)
    turn = await turns_repo.create_main_turn(
        conn,
        session_id=session_id,
        ordinal=1,
        main_index=1,
        question_type="hr",
        topic=first_entry.topic,
        question_text=first_entry.question or first_entry.fallback_question,
        rubric=first_entry.rubric.model_dump(mode="json", by_alias=True),
        catalog_entry_id=first_entry.id,
        difficulty=None,
    )
    await sessions_repo.set_current_turn(conn, session_id, turn["id"])
    session = await sessions_repo.transition(
        conn,
        session_id,
        SessionStatus.delivering_question,
        expected_statuses=(SessionStatus.preparing,),
    )

    started = messages.SESSION_STARTED.format(
        role=messages.ROLE_LABELS[role.value],
        level=messages.LEVEL_LABELS[level.value],
        fmt=messages.FORMAT_LABELS[fmt.value],
    )
    await _enqueue(
        conn,
        unique_key=f"ui:{update_id}:started",
        user=user,
        payload=text_payload(user["chat_id"], started),
        session_id=session_id,
    )
    await enqueue_question_with_controls(
        conn, config, user=user, session_id=session_id, turn=turn, session=session
    )
    await events_repo.emit(
        conn,
        dedup_key=f"start:{session_id}",
        name="start",
        user_id=user["id"],
        session_id=session_id,
        props_category={"role": role.value, "level": level.value, "format": fmt.value},
    )
    await users_repo.set_ui_state(conn, user["id"], "in_session")
    return session


async def enqueue_question_with_controls(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session_id: uuid.UUID,
    session: asyncpg.Record | None,
    turn: asyncpg.Record,
) -> None:
    """Вопрос (ForceReply) + отдельное сообщение управления (§3.7):
    ForceReply и InlineKeyboardMarkup нельзя задавать одновременно [S1]."""
    session_row = session or await sessions_repo.find_by_id(conn, session_id)
    question_text = build_question_text(config, session_row, turn)
    await _enqueue(
        conn,
        unique_key=f"question:{turn['id']}",
        user=user,
        payload=force_reply_payload(user["chat_id"], question_text),
        kind="question_text",
        session_id=session_id,
    )

    ttl = _question_control_ttl(config)
    buttons = []
    for action, label, allowed_value in (
        ("skip_turn", messages.SKIP_LABEL, str(turn["id"])),
        ("finish_session", messages.FINISH_LABEL, str(turn["id"])),
        ("cancel_session", messages.CANCEL_LABEL, str(turn["id"])),
    ):
        token = generate_ui_token()
        await ui_tokens_repo.issue(
            conn,
            token=token,
            user_id=user["id"],
            action=action,
            ttl=ttl,
            allowed_value=allowed_value,
            session_id=session_id,
        )
        buttons.append([(label, f"{action}:{token}")])
    await _enqueue(
        conn,
        unique_key=f"control:{turn['id']}",
        user=user,
        payload=buttons_payload(user["chat_id"], messages.CONTROL_MESSAGE, buttons),
        kind="buttons",
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Приём текстового ответа (§2.2, §3.3; транзакционная половина T09)
# ---------------------------------------------------------------------------


async def accept_text_answer_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    message: dict,
    update_id: int,
) -> None:
    """Проверяет reply, владельца, текущий turn; нормализует и принимает ответ.

    В одной транзакции: ответ + версия сессии + уникальное assess-задание +
    подтверждение в outbox (§2.2). Двойной ответ после первого принятия ->
    BUSY без нового задания и без перезаписи (§2.4).
    """
    if session["status"] != SessionStatus.awaiting_answer.value:
        raise DomainError(ErrorCode.BUSY)
    if session["current_turn_id"] is None:
        raise DomainError(ErrorCode.REPLY_REQUIRED, message=messages.REPLY_REQUIRED_HINT)

    turn = await turns_repo.find_by_id(conn, session["current_turn_id"], for_update=True)
    if turn is None or turn["answer_text"] is not None or turn["skipped"]:
        raise DomainError(ErrorCode.BUSY)

    # reply на подтверждённый message_id текущего вопроса или его известной копии
    reply_to = (message.get("reply_to_message") or {}).get("message_id")
    known_ids: set[int] = set()
    if turn["delivered_message_id"] is not None:
        known_ids.add(turn["delivered_message_id"])
    known_ids |= await outbox_repo.known_message_ids(conn, [f"question:{turn['id']}"])
    if reply_to is None or reply_to not in known_ids:
        raise DomainError(ErrorCode.REPLY_REQUIRED, message=messages.REPLY_REQUIRED_HINT)

    # нормализация и граничные длины — до каких-либо изменений (§3.3)
    normalized = validate_answer_text(message.get("text") or "")
    ack_payload = reply_payload(
        user["chat_id"], messages.ANSWER_ACCEPTED, message["message_id"]
    )
    await _accept_normalized(
        conn,
        config,
        user=user,
        session=session,
        turn=turn,
        normalized=normalized,
        ack_payload=ack_payload,
        expected_from=(SessionStatus.awaiting_answer,),
        source="text",
    )


async def accept_confirmed_transcript_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    expected_transcript_version: int,
) -> None:
    """Подтверждённая расшифровка принимается тем же потоком, что и текст
    (§3.5). Подтверждение старой версии отклоняется (transcript_version)."""
    if session["status"] != SessionStatus.confirming_transcript.value:
        raise DomainError(ErrorCode.BUSY)
    if turn["transcript_version"] != expected_transcript_version:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    draft = turn["draft_transcript"]
    if not draft:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    normalized = validate_answer_text(draft)
    ack_payload = text_payload(user["chat_id"], messages.ANSWER_ACCEPTED)
    await _accept_normalized(
        conn,
        config,
        user=user,
        session=session,
        turn=turn,
        normalized=normalized,
        ack_payload=ack_payload,
        expected_from=(SessionStatus.confirming_transcript,),
        source="voice",
        clear_draft=True,
    )
    from app.services import metrics

    metrics.REGISTRY.inc(metrics.STT_ACTION, action="confirmed")


async def _accept_normalized(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    normalized: str,
    ack_payload: dict,
    expected_from: tuple[SessionStatus, ...],
    source: str,
    clear_draft: bool = False,
) -> None:
    """Атомарно: ответ + версия сессии + уникальное assess-задание +
    подтверждение в outbox (§2.2). Двойное принятие -> BUSY."""
    saved = await turns_repo.save_answer(conn, turn["id"], normalized)
    if not saved:
        raise DomainError(ErrorCode.BUSY)
    if clear_draft:
        await conn.execute(
            "UPDATE turns SET draft_transcript = NULL WHERE id = $1", turn["id"]
        )

    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.evaluating,
        expected_statuses=expected_from,
        touch_user_action=True,
        inactive_ttl=_inactive_ttl(config),
    )
    if updated is None:
        # конкурентный переход (гонка двух ответов): ответ уже сохранён — BUSY
        raise DomainError(ErrorCode.BUSY)

    await jobs_repo.create(
        conn,
        unique_key=f"assess:{turn['id']}",
        session_id=session["id"],
        turn_id=turn["id"],
        kind="assess",
        expected_version=updated["version"],
        payload={"turn_id": str(turn["id"]), "session_id": str(session["id"])},
    )
    await _enqueue(
        conn,
        unique_key=f"ack:{turn['id']}",
        user=user,
        payload=ack_payload,
        session_id=session["id"],
    )
    await events_repo.emit(
        conn,
        dedup_key=f"answer_accepted:{turn['id']}",
        name="answer_accepted",
        user_id=user["id"],
        session_id=session["id"],
        props_numeric={"length": len(normalized)},
        props_category={"question_type": turn["question_type"], "source": source},
    )
    if await turns_repo.count_answered(conn, session["id"]) == 1:
        await events_repo.emit(
            conn,
            dedup_key=f"first_answer:{session['id']}",
            name="first_answer",
            user_id=user["id"],
            session_id=session["id"],
        )


# ---------------------------------------------------------------------------
# Пропуск, следующий вопрос, завершение плана (§3.3)
# ---------------------------------------------------------------------------


async def submit_voice_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    voice_meta: dict,
    update_id: int,
) -> None:
    """Приём голосового ответа (§3.5, задача T15): метаданные проверяются
    ДО скачивания; фактическую длительность позже подтвердит декодер.
    Лимит 3 запуска распознавания на вопрос не сбрасывается перезапуском
    worker (счётчик в БД)."""
    limits = config.settings.limits
    duration = int(voice_meta.get("duration") or 0)
    if duration > limits.voice_max_seconds:
        raise DomainError(ErrorCode.VOICE_TOO_LONG)
    file_size = voice_meta.get("file_size")
    if isinstance(file_size, int) and file_size > limits.voice_max_bytes:
        raise DomainError(ErrorCode.VOICE_TOO_LARGE)
    file_id = voice_meta.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise DomainError(ErrorCode.VOICE_FORMAT_UNSUPPORTED)

    from app.services.media_service import MediaStorageFullError, check_capacity

    try:
        check_capacity(
            config.env.media_dir,
            limits.media_total_max_mib,
            incoming_bytes=limits.voice_max_bytes,
        )
    except MediaStorageFullError:
        # При заполнении отказать новому голосовому входу без принятия ответа
        raise DomainError(ErrorCode.MEDIA_STORAGE_FULL) from None

    # Канонический порядок блокировок (§2.3): sessions -> turns
    session_locked = await sessions_repo.find_by_id(conn, session["id"], for_update=True)
    if (
        session_locked is None
        or session_locked["status"] != SessionStatus.awaiting_answer.value
        or session_locked["current_turn_id"] != turn["id"]
    ):
        raise DomainError(ErrorCode.BUSY)
    turn_locked = await turns_repo.find_by_id(conn, turn["id"], for_update=True)
    if (
        turn_locked is None
        or turn_locked["answer_text"] is not None
        or turn_locked["skipped"]
    ):
        raise DomainError(ErrorCode.BUSY)
    new_attempts = await conn.fetchval(
        """
        UPDATE turns SET stt_attempts = stt_attempts + 1
        WHERE id = $1 AND stt_attempts < $2
        RETURNING stt_attempts
        """,
        turn["id"],
        limits.stt_attempts_per_turn,
    )
    if new_attempts is None:
        raise DomainError(
            ErrorCode.TRANSCRIPTION_FAILED, message=messages.STT_ATTEMPTS_EXHAUSTED
        )
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.transcribing,
        expected_statuses=(SessionStatus.awaiting_answer,),
        touch_user_action=True,
        inactive_ttl=_inactive_ttl(config),
    )
    if updated is None:
        raise DomainError(ErrorCode.BUSY)
    await jobs_repo.create(
        conn,
        unique_key=f"transcribe:{turn['id']}:{new_attempts}",
        session_id=session["id"],
        turn_id=turn["id"],
        kind="transcribe",
        expected_version=updated["version"],
        payload={"file_id": file_id, "duration": duration},
    )
    await _enqueue(
        conn,
        unique_key=f"ui:{update_id}:transcribing",
        user=user,
        payload=text_payload(user["chat_id"], messages.TRANSCRIBING),
        session_id=session["id"],
    )


async def restart_voice_recording_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    update_id: int,
) -> None:
    """«Записать заново» из confirming_transcript: возврат в awaiting_answer,
    прежние кнопки черновика недействительны (§2.4, §3.5)."""
    if session["status"] != SessionStatus.confirming_transcript.value:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.awaiting_answer,
        expected_statuses=(SessionStatus.confirming_transcript,),
        touch_user_action=True,
        inactive_ttl=_inactive_ttl(config),
    )
    if updated is None:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    await conn.execute(
        "UPDATE turns SET draft_transcript = NULL WHERE id = $1", turn["id"]
    )
    from app.services import metrics

    metrics.REGISTRY.inc(metrics.STT_ACTION, action="rerecorded")
    await ui_tokens_repo.revoke_unconsumed(
        conn,
        user["id"],
        actions=("confirm_transcript", "edit_transcript", "rerecord_voice"),
    )
    await _enqueue(
        conn,
        unique_key=f"ui:{update_id}:rerecord",
        user=user,
        payload=text_payload(user["chat_id"], messages.VOICE_RERECORD_PROMPT),
        session_id=session["id"],
    )


async def skip_turn_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    update_id: int,
) -> None:
    if session["status"] not in (
        SessionStatus.awaiting_answer.value,
        SessionStatus.confirming_transcript.value,
    ):
        raise DomainError(ErrorCode.BUSY)
    if turn["answer_text"] is not None or turn["skipped"]:
        raise DomainError(ErrorCode.BUSY)

    marked = await turns_repo.mark_skipped(conn, turn["id"])
    if not marked:
        raise DomainError(ErrorCode.BUSY)

    base_index = turn["main_index"]
    if turn["kind"] == TurnKind.follow_up.value:
        parent = await turns_repo.find_by_id(conn, turn["parent_turn_id"])
        base_index = parent["main_index"] if parent else 1
    next_index = (base_index or 1) + 1
    total = config.settings.interview.main_questions

    if next_index <= total:
        await advance_to_main(
            conn,
            config,
            user=user,
            session=session,
            main_index=next_index,
            notice=messages.SKIPPED_QUESTION,
            notice_key=f"ui:{update_id}:notice",
        )
        return

    # план пройден: пропуски без единого ответа -> cancelled + освобождение (§3.3)
    answered = await turns_repo.count_answered(conn, session["id"])
    if answered == 0:
        await cancel_without_answers(conn, config, user=user, session=session)
    else:
        await start_reporting_tx(conn, config, user=user, session=session)


async def advance_to_main(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    main_index: int,
    notice: str | None,
    notice_key: str | None = None,
    question_override: str | None = None,
    difficulty_override: int | None = None,
    expected_from: tuple[SessionStatus, ...] = (
        SessionStatus.awaiting_answer,
        SessionStatus.confirming_transcript,
    ),
    touch_user_action: bool = True,
) -> None:
    """Переход к следующей основной теме: вопрос из каталога либо валидный
    вопрос модели (question_override), сложность — текущая или вычисленная
    шагом (§3.3)."""
    scenario = scenario_for(config, session["role_id"], session["level"])
    entry = entry_by_main_index(scenario, main_index)
    ordinal = await turns_repo.next_ordinal(conn, session["id"])
    if entry.question_type.value == "technical":
        difficulty = (
            difficulty_override
            if difficulty_override is not None
            else session["current_difficulty"]
        )
        question_text = question_override or entry.question_for_difficulty(difficulty)
        question_type = "technical"
    else:
        question_text = entry.question or entry.fallback_question
        difficulty = None
        question_type = "hr"
    turn = await turns_repo.create_main_turn(
        conn,
        session_id=session["id"],
        ordinal=ordinal,
        main_index=main_index,
        question_type=question_type,
        topic=entry.topic,
        question_text=question_text,
        rubric=entry.rubric.model_dump(mode="json", by_alias=True),
        catalog_entry_id=entry.id,
        difficulty=difficulty,
    )
    await sessions_repo.set_current_turn(conn, session["id"], turn["id"])
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.delivering_question,
        expected_statuses=expected_from,
        touch_user_action=touch_user_action,
        inactive_ttl=_inactive_ttl(config),
    )
    if notice and notice_key:
        await _enqueue(
            conn,
            unique_key=notice_key,
            user=user,
            payload=text_payload(user["chat_id"], notice),
            session_id=session["id"],
        )
    await enqueue_question_with_controls(
        conn, config, user=user, session_id=session["id"], session=updated, turn=turn
    )


async def cancel_without_answers(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
) -> None:
    """Все вопросы пропущены / завершение без ответов: cancelled, резерв
    освобождён, попытка сохранена (§3.3, §3.7)."""
    await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.cancelled,
        expected_statuses=tuple(
            s for s in SessionStatus if s.value not in TERMINAL
        ),
    )
    await credits_repo.release_by_session(conn, session["id"])
    await jobs_repo.cancel_by_session(conn, session["id"])
    await outbox_repo.cancel_pending_by_session(conn, session["id"])
    await users_repo.set_ui_state(conn, user["id"], "idle")
    await _enqueue(
        conn,
        unique_key=f"ui:{session['id']}:no-answers",
        user=user,
        payload=text_payload(user["chat_id"], messages.ALL_SKIPPED),
        session_id=session["id"],
    )


async def start_reporting_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    touch_user_action: bool = True,
) -> None:
    """Переход в reporting + report-задание (исполнение — часть 4, T13/T14).

    touch_user_action=False для фоновых переходов: фоновые повторы срок
    сессии не продлевают (§1.3)."""
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.reporting,
        expected_statuses=(SessionStatus.awaiting_answer, SessionStatus.evaluating),
        touch_user_action=touch_user_action,
        inactive_ttl=_inactive_ttl(config),
    )
    if updated is None:
        raise DomainError(ErrorCode.BUSY)
    await jobs_repo.create(
        conn,
        unique_key=f"report:{session['id']}",
        session_id=session["id"],
        kind="report",
        expected_version=updated["version"],
        payload={"session_id": str(session["id"])},
    )
    await _enqueue(
        conn,
        unique_key=f"ui:{session['id']}:report-started",
        user=user,
        payload=text_payload(user["chat_id"], messages.PREPARING_REPORT),
        session_id=session["id"],
    )


async def finish_session_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    update_id: int,
) -> None:
    status = session["status"]
    if status in TERMINAL:
        raise DomainError(ErrorCode.STALE_ACTION)
    if status == SessionStatus.reporting.value:
        raise DomainError(ErrorCode.BUSY, message=messages.FINISH_ALREADY_REPORTING)

    answered = await turns_repo.count_answered(conn, session["id"])
    if status == SessionStatus.evaluating.value:
        # «Завершить» во время оценки фиксируется в requested_action;
        # новая тема после окончания оценки не создаётся (§2.4). Текущая
        # оценка остаётся валидной: expected_version задания синхронизируется.
        new_version = await sessions_repo.set_requested_action(
            conn, session["id"], "finish"
        )
        await jobs_repo.sync_expected_version(conn, session["id"], new_version)
        await _enqueue(
            conn,
            unique_key=f"ui:{update_id}:finish-recorded",
            user=user,
            payload=text_payload(user["chat_id"], messages.FINISH_RECORDED),
            session_id=session["id"],
        )
        return
    if answered == 0:
        # NO_ANSWERS: завершение без отчёта; резерв освобождён (§3.1, §3.3)
        await cancel_without_answers(conn, config, user=user, session=session)
        return
    # «Завершить» из awaiting_answer: текущий неотвеченный вопрос помечается
    # пропущенным — снимок отчёта включает только отвеченные и пропущенные
    # turns (§3.3, §3.4)
    if session["current_turn_id"] is not None:
        await turns_repo.mark_skipped(conn, session["current_turn_id"])
    await start_reporting_tx(conn, config, user=user, session=session)


# ---------------------------------------------------------------------------
# Отмена и удаление (§2.2, §2.4, §3.7)
# ---------------------------------------------------------------------------


async def cancel_session_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    update_id: int,
) -> None:
    status = session["status"]
    if status in TERMINAL:
        raise DomainError(ErrorCode.STALE_ACTION)
    if status == SessionStatus.reporting.value:
        delivery_started = await conn.fetchval(
            "SELECT delivery_started_at FROM reports WHERE session_id = $1", session["id"]
        )
        if delivery_started is not None:
            # после первой попытки отправки отчёта отмена возвращает BUSY (§2.4)
            raise DomainError(ErrorCode.BUSY, message=messages.CANCEL_ALREADY_SENDING)

    await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.cancelled,
        expected_statuses=tuple(s for s in SessionStatus if s.value not in TERMINAL),
    )
    await credits_repo.release_by_session(conn, session["id"])
    await jobs_repo.cancel_by_session(conn, session["id"])
    await outbox_repo.cancel_pending_by_session(conn, session["id"])
    await users_repo.set_ui_state(conn, user["id"], "idle")
    await _enqueue(
        conn,
        unique_key=f"ui:{update_id}:cancelled",
        user=user,
        payload=text_payload(user["chat_id"], messages.CANCELLED),
        session_id=session["id"],
    )


async def request_delete_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    update_id: int,
) -> datetime:
    """Подтверждённое удаление: блокировка новых действий, отмена активной
    сессии и очередей, признак deletion_requested_at (§2.2, §3.7).

    Физическое удаление содержания, медиа и tombstone выполняет задача T18
    (часть 5); срок — не более 24 часов.
    """
    deadline = _utcnow() + timedelta(hours=config.settings.retention.deletion_deadline_hours)
    active = await sessions_repo.find_active(conn, user["id"], for_update=True)
    if active is not None:
        await sessions_repo.transition(
            conn,
            active["id"],
            SessionStatus.cancelled,
            expected_statuses=tuple(s for s in SessionStatus if s.value not in TERMINAL),
        )
        await credits_repo.release_by_session(conn, active["id"])
        await jobs_repo.cancel_by_session(conn, active["id"])
    await outbox_repo.cancel_pending_by_user(conn, user["id"])
    await users_repo.request_deletion(conn, user["id"])
    await ui_tokens_repo.revoke_unconsumed(
        conn,
        user["id"],
        actions=(
            "skip_turn",
            "finish_session",
            "cancel_session",
            "cancel_confirm",
            "cancel_decline",
            "start_session",
            "select_role",
            "select_level",
            "select_format",
            "back_to_role",
            "back_to_level",
            "back_to_format",
            "consent_accept",
            "consent_decline",
            "delete_decline",
        ),
    )
    await _enqueue(
        conn,
        unique_key=f"ui:{update_id}:delete-accepted",
        user=user,
        payload=text_payload(user["chat_id"], messages.DELETE_ACCEPTED),
    )
    return deadline


# ---------------------------------------------------------------------------
# Представления (§3.1: SessionView) и статус
# ---------------------------------------------------------------------------


async def build_session_view(
    conn: asyncpg.Connection, config: AppConfig, session: asyncpg.Record
) -> SessionView:
    turn_view: TurnView | None = None
    if session["current_turn_id"] is not None:
        turn = await turns_repo.find_by_id(conn, session["current_turn_id"])
        if turn is not None:
            turn_view = TurnView(
                turn_id=turn["id"],
                ordinal=turn["ordinal"],
                kind=TurnKind(turn["kind"]),
                main_index=turn["main_index"],
                topic=turn["topic"],
                question_text=turn["question_text"],
                difficulty=turn["difficulty"],
                delivered_message_id=turn["delivered_message_id"],
            )
    answered = await turns_repo.count_answered(conn, session["id"])
    skipped = await turns_repo.count_skipped(conn, session["id"])
    return SessionView(
        session_id=session["id"],
        role_id=RoleId(session["role_id"]),
        level=Level(session["level"]),
        format=InterviewFormat(session["format"]),
        status=SessionStatus(session["status"]),
        version=session["version"],
        current_turn=turn_view,
        main_total=config.settings.interview.main_questions,
        answered_count=answered,
        skipped_count=skipped,
        follow_ups_used=session["follow_ups_used"],
        created_at=session["created_at"],
        expires_at=session["expires_at"],
    )


_STATUS_TEXTS = {
    "preparing": messages.STATUS_STARTING,
    "delivering_question": messages.STATUS_DELIVERING,
    "awaiting_answer": messages.STATUS_AWAITING_ANSWER,
    "transcribing": messages.STATUS_TRANSCRIBING,
    "confirming_transcript": messages.STATUS_CONFIRMING_TRANSCRIPT,
    "evaluating": messages.STATUS_EVALUATING,
    "reporting": messages.STATUS_REPORTING,
    "completed": "завершена",
    "cancelled": "отменена",
    "expired": "истекла по неактивности",
    "failed": "завершена технической ошибкой",
}


async def build_status_text(
    conn: asyncpg.Connection, config: AppConfig, session: asyncpg.Record
) -> str:
    view = await build_session_view(conn, config, session)
    return messages.STATUS_ACTIVE.format(
        role=messages.ROLE_LABELS[view.role_id.value],
        level=messages.LEVEL_LABELS[view.level.value],
        fmt=messages.FORMAT_LABELS[view.format.value],
        state=_STATUS_TEXTS.get(session["status"], session["status"]),
        answered=view.answered_count,
        skipped=view.skipped_count,
        total=view.main_total,
        follow_ups=view.follow_ups_used,
    )


# ---------------------------------------------------------------------------
# Хуки доставки outbox (§2.2, §2.4): подтверждение вопроса, отказ доставки
# ---------------------------------------------------------------------------


async def enqueue_notice(
    conn: asyncpg.Connection,
    *,
    user: asyncpg.Record,
    text: str,
    unique_key: str,
    session_id: uuid.UUID | None = None,
) -> None:
    """Служебное текстовое уведомление (идемпотентно по unique_key)."""
    await _enqueue(
        conn,
        unique_key=unique_key,
        user=user,
        payload=text_payload(user["chat_id"], text),
        session_id=session_id,
    )


async def on_outbox_delivered_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    row: asyncpg.Record,
    message_id: int,
    *,
    session_locked: asyncpg.Record | None,
) -> None:
    """Подтверждение Bot API: message_id сохранён; delivering_question ->
    awaiting_answer (§2.4). Поздний результат отменённой сессии игнорируется.

    Вызывается после блокировки users/sessions в каноническом порядке (§2.3);
    параметры — свежие записи под блокировкой.
    """
    if row["session_id"] is None or session_locked is None:
        return
    if row["kind"] == "question_text":
        try:
            turn_id = uuid.UUID(row["unique_key"].split(":", 1)[1])
        except (IndexError, ValueError):
            return  # служебные/тестовые строки без ссылки на turn
        await turns_repo.set_delivered_message_id(conn, turn_id, message_id)
        if session_locked["status"] == SessionStatus.delivering_question.value:
            updated = await sessions_repo.transition(
                conn,
                session_locked["id"],
                SessionStatus.awaiting_answer,
                expected_statuses=(SessionStatus.delivering_question,),
            )
            session_row = updated or session_locked
        else:
            session_row = session_locked
        # В voice-формате доставленный текстовый вопрос дополнительно
        # озвучивается TTS (§3.5); сбой озвучивания не блокирует ответ
        if session_row["format"] == "voice":
            await jobs_repo.create(
                conn,
                unique_key=f"tts:{turn_id}",
                session_id=session_row["id"],
                turn_id=turn_id,
                kind="tts",
                expected_version=session_row["version"],
                payload={"turn_id": str(turn_id)},
            )
        return
    if row["kind"] == "report_part":
        from app.services import report_service

        user_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", session_locked["user_id"]
        )
        if user_row is not None:
            await report_service.on_report_part_delivered_tx(
                conn,
                config,
                row,
                message_id,
                user=user_row,
                session=session_locked,
            )


async def on_outbox_failed_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    row: asyncpg.Record,
    error: str,
    *,
    chat_reachable: bool,
    user_locked: asyncpg.Record | None,
    session_locked: asyncpg.Record | None,
) -> None:
    """Окончательный сбой доставки обязательной строки.

    Вопрос: сессия failed, резерв освобождается, уведомление только при
    доступности чата (§2.4, §3.2 DELIVERY_FAILED). Отчёт: то же правило —
    резерв освобождается независимо от уже видимой части.
    Вызывается при уже заблокированных users/sessions.
    """
    if row["session_id"] is None or session_locked is None or user_locked is None:
        return
    session = session_locked
    if session["status"] in TERMINAL:
        return
    if row["kind"] == "question_text" and session["status"] in (
        SessionStatus.preparing.value,
        SessionStatus.delivering_question.value,
    ):
        await sessions_repo.transition(
            conn,
            session["id"],
            SessionStatus.failed,
            expected_statuses=(SessionStatus.preparing, SessionStatus.delivering_question),
        )
    elif row["kind"] == "report_part" and session["status"] == SessionStatus.reporting.value:
        await sessions_repo.transition(
            conn,
            session["id"],
            SessionStatus.failed,
            expected_statuses=(SessionStatus.reporting,),
        )
    else:
        return
    await credits_repo.release_by_session(conn, session["id"])
    await jobs_repo.cancel_by_session(conn, session["id"])
    await outbox_repo.cancel_pending_by_session(conn, session["id"])
    await users_repo.set_ui_state(conn, user_locked["id"], "idle")
    if chat_reachable:
        await _enqueue(
            conn,
            unique_key=f"delivery-failed:{session['id']}",
            user=user_locked,
            payload=text_payload(user_locked["chat_id"], messages.QUESTION_DELIVERY_FAILED),
            session_id=session["id"],
        )
