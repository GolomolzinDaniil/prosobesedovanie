"""Диспетчер обновлений worker-а (§2.2, §3.1, §3.2, §3.7; задачи T06, T07).

Обработчик транзакционный: вызывается внутри транзакции inbox-воркера при
заблокированной строке пользователя. Ожидаемые доменные ошибки превращаются
в сообщения пользователю (конкретная причина и одно доступное действие);
неожиданные — откатывают транзакцию и повторяются (до 5 раз, §3.2), не
повторяя уже сохранённые доменные эффекты (unique_key outbox/jobs/events).

Проверки на входе (§2.2): личный чат, согласие, владелец callback и параметры.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import UTC, timedelta
from typing import Any

import asyncpg

from app.config import AppConfig
from app.domain.errors import DomainError
from app.services import session_service, ui_flow
from app.storage import (
    credits_repo,
    events_repo,
    inbox_repo,
    outbox_repo,
    sessions_repo,
    turns_repo,
    ui_tokens_repo,
    users_repo,
)
from app.telegram import messages
from app.telegram.bot import BotApiClient
from app.telegram.markup import buttons_payload, text_payload
from contracts.models import ErrorCode, InterviewFormat, Level, RoleId, SessionStatus

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "cancelled", "expired", "failed")

COMMANDS = ("start", "help", "status", "report", "balance", "delete", "buy", "terms", "support")


class WorkerDeps:
    """Зависимости воркеров: конфигурация, порт Bot API, LLM-клиент, лимитер.

    llm=None при незаполненном provider-profile — AI-вызовы заблокированы,
    задания оценки уходят в запасной сценарий (§1.4, §3.2)."""

    def __init__(
        self,
        config: AppConfig,
        bot: BotApiClient,
        limiter: Any = None,
        llm: Any = None,
        stt: Any = None,
        tts: Any = None,
    ) -> None:
        self.config = config
        self.bot = bot
        self.limiter = limiter
        self.llm = llm
        self.stt = stt
        self.tts = tts


# ---------------------------------------------------------------------------
# Фаза 1: быстрые действия вне транзакции (§3.1: answerCallbackQuery быстро)
# ---------------------------------------------------------------------------


async def pre_process(deps: WorkerDeps, item: asyncpg.Record) -> None:
    """Некритичные внешние вызовы до доменной транзакции: ack callback и
    предложение открыть личный чат в группе. Идемпотентность: повторный ack
    Telegram игнорирует; групповое уведомление — только на первой попытке."""
    payload = _payload(item)
    if payload is None:
        return
    if item["kind"] == "callback_query":
        cb = payload.get("callback_query") or {}
        cb_id = cb.get("id")
        if isinstance(cb_id, str):
            await deps.bot.answer_callback_query(cb_id)
        return
    if item["kind"] == "message":
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        text = message.get("text") or ""
        if chat.get("type") != "private" and text.startswith("/start") and item["attempts"] <= 1:
            from app.telegram.bot import SendOutcome

            result = await deps.bot.send_message(
                text_payload(chat.get("id"), messages.GROUP_CHAT_NOTICE)
            )
            if result.outcome is not SendOutcome.success:
                logger.info(
                    "group notice not delivered", extra={"outcome": result.outcome.value}
                )


def _payload(item: asyncpg.Record) -> dict[str, Any] | None:
    raw = item["payload"]
    if raw is None:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


# ---------------------------------------------------------------------------
# Фаза 2: доменная обработка внутри транзакции
# ---------------------------------------------------------------------------


async def handle_update(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record
) -> None:
    payload = _payload(item)
    if payload is None:
        # payload уже обнулён политикой хранения — эффект не повторяем (§3.9)
        logger.info("inbox payload purged, nothing to do", extra={"update_id": item["update_id"]})
        return
    if item["kind"] == "callback_query":
        await _handle_callback(conn, deps, item, payload["callback_query"])
    elif item["kind"] == "message":
        await _handle_message(conn, deps, item, payload["message"])
    else:
        # неподдерживаемые виды не доходят до worker (webhook: skipped)
        logger.info("unsupported kind in worker", extra={"kind": item["kind"]})


async def _handle_message(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, message: dict[str, Any]
) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        # В группе ответы не сохраняются; уведомление отправлено в фазе 1 (§1.3)
        return
    from_block = message.get("from") or {}
    telegram_user_id = from_block.get("id")
    if not isinstance(telegram_user_id, int):
        return  # служебное сообщение без автора — доменных действий нет

    text = message.get("text")
    is_command = isinstance(text, str) and text.startswith("/")

    user = await users_repo.lock_by_telegram_id(conn, telegram_user_id)
    if user is None:
        if is_command and text.split()[0][1:].split("@", 1)[0].lower() == "start":
            user = await _create_profile(conn, deps, message)
        elif is_command and text.split()[0][1:].split("@", 1)[0].lower() == "help":
            # справка доступна до входа без создания профиля (§3.7, §3.9)
            await _direct_hint(conn, deps, item, chat["id"], messages.HELP_TEXT)
            return
        else:
            # Произвольный текст/команды без профиля: подсказка (профиль не
            # создаём, данные до согласия не собираем)
            await _direct_hint(conn, deps, item, chat["id"], messages.NO_PROFILE_HINT)
            return

    if await _check_rate_limit(conn, deps, item, user):
        return
    if user["deletion_requested_at"] is not None:
        await _enqueue_text(
            conn, deps, item, user, messages.DELETION_IN_PROGRESS, suffix="deletion"
        )
        return

    if is_command:
        await _handle_command(conn, deps, item, user, message, text)
        return
    await _handle_plain_message(conn, deps, item, user, message)


async def _create_profile(
    conn: asyncpg.Connection, deps: WorkerDeps, message: dict[str, Any]
) -> asyncpg.Record:
    """Профиль создаётся по команде в личном чате: только Telegram ID и chat ID
    (§3.9). Кампания source_code — только из allowlist (§3.7)."""
    text = (message.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    source_code: str | None = None
    if len(parts) == 2:
        candidate = parts[1].strip()
        allowlist = deps.config.settings.campaign_source_allowlist
        if candidate in allowlist and len(candidate) <= 32:
            source_code = candidate
    return await users_repo.create(
        conn,
        telegram_user_id=message["from"]["id"],
        chat_id=message["chat"]["id"],
        ui_state="new",
        source_code=source_code,
    )


async def _direct_hint(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    chat_id: int,
    text: str,
) -> None:
    """Подсказка без профиля: outbox требует user_id, отправляем напрямую.
    Идемпотентность по update_id: только первая попытка обработчика."""
    if item["attempts"] > 1:
        return
    await deps.bot.send_message(text_payload(chat_id, text))


# ---------------------------------------------------------------------------
# Лимит команд (§1.3: 10 команд в минуту, транзакционный контроль)
# ---------------------------------------------------------------------------


async def _check_rate_limit(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, user: asyncpg.Record
) -> bool:
    """True — действие отклонено лимитом (сообщение поставлено в очередь)."""
    await inbox_repo.attach_user(conn, item["update_id"], user["id"])
    limit = deps.config.settings.limits.commands_per_minute
    row = await conn.fetchrow(
        """
        SELECT count(*)::int AS n, min(received_at) AS oldest
        FROM inbox
        WHERE user_id = $1 AND received_at > now() - interval '60 seconds'
        """,
        user["id"],
    )
    if row is None or row["n"] <= limit:
        return False
    retry_after = 60
    if row["oldest"] is not None:
        oldest = row["oldest"]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        from datetime import datetime

        retry_after = max(
            1, math.ceil(60 - (datetime.now(UTC) - oldest).total_seconds())
        )
    await _enqueue_text(
        conn,
        deps,
        item,
        user,
        messages.COMMANDS_RATE_LIMITED.format(seconds=retry_after),
        suffix="ratelimit",
    )
    return True


# ---------------------------------------------------------------------------
# Команды (§3.7)
# ---------------------------------------------------------------------------


async def _handle_command(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    message: dict[str, Any],
    text: str,
) -> None:
    command = text.split()[0][1:].split("@", 1)[0].lower()
    if command == "start":
        await _cmd_start(conn, deps, item, user)
    elif command == "help":
        await _enqueue_text(conn, deps, item, user, messages.HELP_TEXT, suffix="help")
    elif command == "status":
        await _cmd_status(conn, deps, item, user)
    elif command == "report":
        await _cmd_report(conn, deps, item, user)
    elif command == "balance":
        await _cmd_balance(conn, deps, item, user)
    elif command == "delete":
        await ui_flow.render_delete_confirmation(
            conn, deps.config, user=user, update_id=item["update_id"]
        )
    elif command in ("buy", "terms", "support", "paysupport"):
        # Коммерческий этап выключен (§3.8): команды появятся с оплатой
        await _enqueue_text(conn, deps, item, user, messages.BILLING_DISABLED, suffix="billing")
    else:
        await _enqueue_text(conn, deps, item, user, messages.UNKNOWN_COMMAND, suffix="unknown")


async def _cmd_start(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, user: asyncpg.Record
) -> None:
    active = await sessions_repo.find_active(conn, user["id"])
    if active is not None:
        status_text = await session_service.build_status_text(conn, deps.config, active)
        await _enqueue_text(
            conn,
            deps,
            item,
            user,
            f"{messages.ACTIVE_SESSION_HINT}\n\n{status_text}",
            suffix="active",
        )
        return
    if not user["consent_version"]:
        await ui_flow.render_consent(conn, deps.config, user=user, update_id=item["update_id"])
        return
    await ui_flow.render_role_selection(
        conn, deps.config, user=user, update_id=item["update_id"]
    )


async def _cmd_status(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, user: asyncpg.Record
) -> None:
    active = await sessions_repo.find_active(conn, user["id"])
    if active is None:
        await _enqueue_text(
            conn, deps, item, user, messages.NO_ACTIVE_SESSION, suffix="no-session"
        )
        return
    status_text = await session_service.build_status_text(conn, deps.config, active)
    await _enqueue_text(conn, deps, item, user, status_text, suffix="status")


async def _cmd_report(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, user: asyncpg.Record
) -> None:
    """/report возвращает сохранённый результат без нового LLM-вызова (§2.2)."""
    active = await sessions_repo.find_active(conn, user["id"])
    if active is not None and active["status"] == SessionStatus.reporting.value:
        await _enqueue_text(
            conn, deps, item, user, messages.REPORT_NOT_READY_MSG, suffix="report-wait"
        )
        return
    retention_days = deps.config.settings.retention.completed_content_days
    report = await conn.fetchrow(
        """
        SELECT r.id, r.parts, r.created_at
        FROM reports r
        JOIN sessions s ON s.id = r.session_id
        WHERE s.user_id = $1
          AND r.delivered_at IS NOT NULL
          AND r.created_at > now() - make_interval(days => $2)
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        user["id"],
        retention_days,
    )
    if report is None:
        expired = await conn.fetchval(
            """
            SELECT 1 FROM reports r
            JOIN sessions s ON s.id = r.session_id
            WHERE s.user_id = $1 AND r.delivered_at IS NOT NULL
              AND r.created_at <= now() - make_interval(days => $2)
            LIMIT 1
            """,
            user["id"],
            retention_days,
        )
        if expired is None:
            # После физической очистки (§3.9) строка отчёта удалена; срок
            # определяется по минимальной терминальной записи сессии (§3.10)
            expired = await conn.fetchval(
                """
                SELECT 1 FROM sessions
                WHERE user_id = $1 AND status IN ('completed', 'failed')
                  AND finished_at <= now() - make_interval(days => $2)
                LIMIT 1
                """,
                user["id"],
                retention_days,
            )
        text = messages.REPORT_EXPIRED if expired else messages.NO_REPORT_YET
        await _enqueue_text(conn, deps, item, user, text, suffix="report-empty")
        return
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    for index, part_text in enumerate(parts):
        await outbox_repo.enqueue(
            conn,
            unique_key=f"report:{report['id']}:part:{index}:{item['update_id']}",
            user_id=user["id"],
            chat_id=user["chat_id"],
            kind="report_part",
            payload=text_payload(user["chat_id"], part_text),
            is_required=True,
        )


async def _cmd_balance(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, user: asyncpg.Record
) -> None:
    available = await credits_repo.count_by_state(conn, user["id"], "available")
    reserved = await credits_repo.count_by_state(conn, user["id"], "reserved")
    trial = (
        messages.BALANCE_TRIAL_USED
        if await credits_repo.trial_used(conn, user["id"])
        else messages.BALANCE_TRIAL_AVAILABLE
    )
    text = messages.BALANCE_TEXT.format(available=available, reserved=reserved, trial=trial)
    if available == 0 and reserved == 0 and not deps.config.settings.flags.billing_enabled:
        text += f"\n\n{messages.NO_CREDIT_BILLING_OFF}"
    await _enqueue_text(conn, deps, item, user, text, suffix="balance")


# ---------------------------------------------------------------------------
# Произвольные сообщения в личном чате
# ---------------------------------------------------------------------------


async def _handle_plain_message(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    message: dict[str, Any],
) -> None:
    session = await sessions_repo.find_active(conn, user["id"], for_update=True)
    if session is None:
        hint = (
            messages.NO_PROFILE_HINT
            if user["ui_state"] == "new"
            else "Отправьте /start, чтобы начать новую тренировку."
        )
        await _enqueue_text(
            conn, deps, item, user, f"{messages.NO_ACTIVE_SESSION}\n{hint}", suffix="no-session"
        )
        return

    status = session["status"]
    if status == SessionStatus.awaiting_answer.value:
        if message.get("voice") is not None:
            if session["format"] == "voice":
                turn = None
                if session["current_turn_id"] is not None:
                    turn = await turns_repo.find_by_id(conn, session["current_turn_id"])
                if turn is None:
                    await _enqueue_text(
                        conn, deps, item, user, messages.BUSY_WAIT_MSG, suffix="busy"
                    )
                    return
                await session_service.submit_voice_tx(
                    conn,
                    deps.config,
                    user=user,
                    session=session,
                    turn=turn,
                    voice_meta=message.get("voice") or {},
                    update_id=item["update_id"],
                )
            else:
                await _enqueue_text(
                    conn, deps, item, user, messages.VOICE_IN_TEXT_FORMAT, suffix="voice"
                )
            return
        if any(message.get(k) is not None for k in ("audio", "document", "video", "video_note")):
            text_msg = (
                messages.VOICE_FORMAT_TEXT
                if session["format"] == "voice"
                else messages.UNSUPPORTED_CONTENT
            )
            await _enqueue_text(conn, deps, item, user, text_msg, suffix="content")
            return
        if isinstance(message.get("text"), str):
            await session_service.accept_text_answer_tx(
                conn,
                deps.config,
                user=user,
                session=session,
                message=message,
                update_id=item["update_id"],
            )
            return
        await _enqueue_text(
            conn, deps, item, user, messages.UNSUPPORTED_CONTENT, suffix="content"
        )
        return

    if status == SessionStatus.evaluating.value:
        await _enqueue_text(
            conn, deps, item, user, messages.BUSY_WAIT_MSG, suffix="busy"
        )
        return
    if status == SessionStatus.reporting.value:
        await _enqueue_text(
            conn, deps, item, user, messages.REPORT_NOT_READY_MSG, suffix="report-wait"
        )
        return
    if status == SessionStatus.transcribing.value:
        await _enqueue_text(conn, deps, item, user, messages.TRANSCRIBING, suffix="busy")
        return
    if status == SessionStatus.confirming_transcript.value:
        if (user["ui_state"] or "").startswith("editing_transcript:"):
            await _handle_transcript_edit(conn, deps, item, user, session, message)
        else:
            await _enqueue_text(
                conn, deps, item, user, messages.TRANSCRIPT_CONFIRM_HINT, suffix="busy"
            )
        return
    if status in (
        SessionStatus.preparing.value,
        SessionStatus.delivering_question.value,
    ):
        await _enqueue_text(conn, deps, item, user, messages.BUSY_WAIT_MSG, suffix="busy")
        return
    # терминальный статус активной сессии невозможен (find_active), защита:
    await _enqueue_text(conn, deps, item, user, messages.NO_ACTIVE_SESSION, suffix="no-session")


async def _handle_transcript_edit(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    session: asyncpg.Record,
    message: dict[str, Any],
) -> None:
    """«Исправить»: текст reply на сообщение черновика проходит те же проверки
    и становится новым черновиком (transcript_version+1); прежние кнопки
    недействительны (§3.5, §3.7). Текст не является ответом до подтверждения."""
    from app.domain.answers import validate_answer_text
    from app.services import voice_service

    state_value = (user["ui_state"] or "").split(":", 1)[1]
    turn_part, _, version_part = state_value.partition(":")
    try:
        turn_id = uuid.UUID(turn_part)
        version = int(version_part)
    except ValueError:
        await users_repo.set_ui_state(conn, user["id"], "in_session")
        await _enqueue_text(conn, deps, item, user, messages.STALE_BUTTON, suffix="stale")
        return
    if not isinstance(message.get("text"), str):
        await _enqueue_text(
            conn, deps, item, user, messages.UNSUPPORTED_CONTENT, suffix="content"
        )
        return
    turn = await turns_repo.find_by_id(conn, turn_id, for_update=True)
    if (
        turn is None
        or session["current_turn_id"] != turn_id
        or turn["transcript_version"] != version
    ):
        await users_repo.set_ui_state(conn, user["id"], "in_session")
        await _enqueue_text(conn, deps, item, user, messages.STALE_BUTTON, suffix="stale")
        return
    draft_row = await outbox_repo.get_by_unique_key(conn, f"transcript:{turn_id}:{version}")
    reply_to = (message.get("reply_to_message") or {}).get("message_id")
    if draft_row is None or draft_row["message_id"] is None or reply_to != draft_row["message_id"]:
        await _enqueue_text(
            conn, deps, item, user, messages.REPLY_REQUIRED_HINT, suffix="reply"
        )
        return
    normalized = validate_answer_text(message["text"])  # EMPTY/TOO_LONG -> notify
    new_version = await conn.fetchval(
        """
        UPDATE turns
        SET draft_transcript = $2, transcript_version = transcript_version + 1
        WHERE id = $1
        RETURNING transcript_version
        """,
        turn_id,
        normalized,
    )
    await users_repo.set_ui_state(conn, user["id"], "in_session")
    await voice_service.render_transcript_confirmation(
        conn,
        deps.config,
        user=user,
        session_id=session["id"],
        turn_id=turn_id,
        draft=normalized,
        version=new_version,
    )
    from app.services import metrics

    metrics.REGISTRY.inc(metrics.STT_ACTION, action="edited")


# ---------------------------------------------------------------------------
# Callback-кнопки (§3.1: токен привязан к владельцу, значению и сроку)
# ---------------------------------------------------------------------------


async def _handle_callback(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, cb: dict[str, Any]
) -> None:
    from_block = cb.get("from") or {}
    telegram_user_id = from_block.get("id")
    if not isinstance(telegram_user_id, int):
        return
    data = cb.get("data")
    if not isinstance(data, str) or ":" not in data:
        return
    action, _, token = data.partition(":")

    user = await users_repo.lock_by_telegram_id(conn, telegram_user_id)
    if user is None:
        return  # чужой/несуществующий профиль: данные не раскрываем (§3.9)
    if await _check_rate_limit(conn, deps, item, user):
        return
    if user["deletion_requested_at"] is not None and action != "delete_decline":
        await _enqueue_text(
            conn, deps, item, user, messages.DELETION_IN_PROGRESS, suffix="deletion"
        )
        return

    record = await ui_tokens_repo.consume(conn, token, telegram_user_id=telegram_user_id)
    if record is None or record["action"] != action:
        # Старая кнопка: ложный успех не показываем, выводим актуальный экран (§3.7)
        await _enqueue_text(conn, deps, item, user, messages.STALE_BUTTON, suffix="stale")
        await ui_flow.render_current_screen(
            conn, deps.config, user=user, update_id=item["update_id"]
        )
        return

    # Доменные ошибки НЕ перехватываются внутри транзакции: воркер откатывает
    # частичные эффекты и отправляет сообщение ошибки в новой транзакции (§2.2)
    await _route_action(conn, deps, item, user, record)


async def _route_action(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    token: asyncpg.Record,
) -> None:
    config = deps.config
    action = token["action"]
    value = token["allowed_value"] or ""
    update_id = item["update_id"]

    if action == "consent_accept":
        if not user["consent_version"]:
            await users_repo.set_consent(
                conn, user["id"], messages.CONSENT_DOCUMENT_VERSION
            )
            # Первое собеседование бесплатно: одна trial-credit при первом
            # согласованном входе (§3.8)
            await credits_repo.ensure_trial_credit(conn, user["id"])
            user = await users_repo.lock_by_id(conn, user["id"])
        await ui_flow.render_role_selection(conn, config, user=user, update_id=update_id)
        return
    if action == "consent_decline":
        await users_repo.set_ui_state(conn, user["id"], "consent_declined")
        await _enqueue_text(
            conn, deps, item, user, messages.CONSENT_DECLINED, suffix="consent-declined"
        )
        return
    if action == "select_role":
        role = _parse_enum(RoleId, value, ErrorCode.UNSUPPORTED_ROLE)
        await ui_flow.render_level_selection(
            conn, config, user=user, update_id=update_id, role=role
        )
        return
    if action == "select_level":
        role_str, _, level_str = value.partition(":")
        role = _parse_enum(RoleId, role_str, ErrorCode.UNSUPPORTED_ROLE)
        level = _parse_enum(Level, level_str, ErrorCode.UNSUPPORTED_LEVEL)
        await ui_flow.render_format_selection(
            conn, config, user=user, update_id=update_id, role=role, level=level
        )
        return
    if action == "select_format":
        role, level, fmt = _parse_triplet(value)
        await ui_flow.render_summary(
            conn, config, user=user, update_id=update_id, role=role, level=level, fmt=fmt
        )
        return
    if action == "back_to_role":
        await ui_flow.render_role_selection(conn, config, user=user, update_id=update_id)
        return
    if action == "back_to_level":
        role = _parse_enum(RoleId, value, ErrorCode.UNSUPPORTED_ROLE)
        await ui_flow.render_level_selection(
            conn, config, user=user, update_id=update_id, role=role
        )
        return
    if action == "back_to_format":
        role_str, _, level_str = value.partition(":")
        role = _parse_enum(RoleId, role_str, ErrorCode.UNSUPPORTED_ROLE)
        level = _parse_enum(Level, level_str, ErrorCode.UNSUPPORTED_LEVEL)
        await ui_flow.render_format_selection(
            conn, config, user=user, update_id=update_id, role=role, level=level
        )
        return
    if action == "start_session":
        role, level, fmt = _parse_triplet(value)
        await session_service.start_session_tx(
            conn, config, user=user, role=role, level=level, fmt=fmt, update_id=update_id
        )
        return
    if action in ("skip_turn", "finish_session", "cancel_session"):
        session, turn = await _bound_session(conn, token)
        if action == "skip_turn":
            if turn is None or str(turn["id"]) != value:
                raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
            await session_service.skip_turn_tx(
                conn, config, user=user, session=session, turn=turn, update_id=update_id
            )
        elif action == "finish_session":
            await session_service.finish_session_tx(
                conn, config, user=user, session=session, update_id=update_id
            )
        else:
            await _render_cancel_confirmation(conn, deps, item, user, session)
        return
    if action == "cancel_confirm":
        session, _ = await _bound_session(conn, token)
        await session_service.cancel_session_tx(
            conn, config, user=user, session=session, update_id=update_id
        )
        return
    if action == "cancel_decline":
        session, turn = await _bound_session(conn, token, allow_terminal=False)
        await _enqueue_text(
            conn, deps, item, user, messages.CONTROL_RERENDER, suffix="control"
        )
        if turn is not None:
            await session_service.enqueue_question_with_controls(
                conn,
                config,
                user=user,
                session_id=session["id"],
                session=session,
                turn=turn,
            )
        return
    if action == "confirm_delete":
        await session_service.request_delete_tx(
            conn, config, user=user, update_id=update_id
        )
        return
    if action == "delete_decline":
        await _enqueue_text(
            conn, deps, item, user, messages.DELETE_DECLINED, suffix="delete-declined"
        )
        return
    if action == "confirm_transcript":
        session, turn = await _bound_session(conn, token)
        turn_part, _, version_part = value.partition(":")
        if turn is None or str(turn["id"]) != turn_part:
            raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
        await session_service.accept_confirmed_transcript_tx(
            conn,
            config,
            user=user,
            session=session,
            turn=turn,
            expected_transcript_version=int(version_part),
        )
        return
    if action == "edit_transcript":
        session, turn = await _bound_session(conn, token)
        turn_part, _, version_part = value.partition(":")
        if turn is None or str(turn["id"]) != turn_part:
            raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
        if turn["transcript_version"] != int(version_part):
            raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
        await users_repo.set_ui_state(
            conn, user["id"], f"editing_transcript:{turn['id']}:{version_part}"
        )
        await _enqueue_text(
            conn, deps, item, user, messages.TRANSCRIPT_EDIT_PROMPT, suffix="edit-prompt"
        )
        return
    if action == "rerecord_voice":
        session, turn = await _bound_session(conn, token)
        if session["status"] != SessionStatus.confirming_transcript.value:
            raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
        await session_service.restart_voice_recording_tx(
            conn, config, user=user, session=session, turn=turn, update_id=update_id
        )
        return
    if action in ("report_rating_useful", "report_rating_not_useful"):
        rating = "useful" if action == "report_rating_useful" else "not_useful"
        await events_repo.emit(
            conn,
            dedup_key=f"report_rating:{token['session_id']}",
            name="report_rating",
            user_id=user["id"],
            session_id=token["session_id"],
            props_category={"rating": rating},
        )
        await _enqueue_text(
            conn, deps, item, user, messages.REPORT_RATING_THANKS, suffix="rating"
        )
        return
    if action == "new_training":
        active = await sessions_repo.find_active(conn, user["id"])
        if active is not None:
            raise DomainError(ErrorCode.ACTIVE_SESSION, message=messages.ACTIVE_SESSION_HINT)
        if not user["consent_version"]:
            await ui_flow.render_consent(conn, config, user=user, update_id=update_id)
            return
        await _enqueue_text(
            conn, deps, item, user, messages.NEW_TRAINING_STARTED_HINT, suffix="new-training"
        )
        await ui_flow.render_role_selection(conn, config, user=user, update_id=update_id)
        return

    raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)


async def _render_cancel_confirmation(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    session: asyncpg.Record,
) -> None:
    """Отмена требует отдельного подтверждения (§3.7); токен на 5 минут."""
    config = deps.config
    if await outbox_repo.get_by_unique_key(
        conn, f"ui:{item['update_id']}:cancel-confirm:0"
    ):
        return
    ttl = timedelta(minutes=config.settings.ui_tokens.cancel_confirm_minutes)
    from app.services.ui_tokens import generate_ui_token

    rows = []
    for action, label in (
        ("cancel_confirm", messages.CANCEL_CONFIRM_LABEL),
        ("cancel_decline", messages.CANCEL_DECLINE_LABEL),
    ):
        token = generate_ui_token()
        await ui_tokens_repo.issue(
            conn,
            token=token,
            user_id=user["id"],
            action=action,
            ttl=ttl,
            allowed_value=str(session["id"]),
            session_id=session["id"],
        )
        rows.append([(label, f"{action}:{token}")])
    await outbox_repo.enqueue(
        conn,
        unique_key=f"ui:{item['update_id']}:cancel-confirm:0",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="buttons",
        payload=buttons_payload(user["chat_id"], messages.CANCEL_CONFIRM, rows),
        session_id=session["id"],
        is_required=True,
    )


async def _bound_session(
    conn: asyncpg.Connection, token: asyncpg.Record, *, allow_terminal: bool = False
) -> tuple[asyncpg.Record, asyncpg.Record | None]:
    """Проверяет привязку токена к активной сессии владельца; старые кнопки
    не управляют новой сессией (§2.4)."""
    session_id = token["session_id"]
    if session_id is None:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    session = await sessions_repo.find_by_id(conn, session_id, for_update=True)
    if session is None or session["user_id"] != token["user_id"]:
        # Не раскрываем наличие чужой сессии (§3.2 NOT_FOUND)
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    if session["status"] in TERMINAL and not allow_terminal:
        raise DomainError(ErrorCode.STALE_ACTION, message=messages.STALE_BUTTON)
    turn = None
    if session["current_turn_id"] is not None:
        turn = await turns_repo.find_by_id(conn, session["current_turn_id"])
    return session, turn


def _parse_enum(enum_type: type, value: str, code: ErrorCode):
    try:
        return enum_type(value)
    except ValueError:
        # Значение из старой конфигурации: специализированный код (§3.1)
        raise DomainError(code) from None


def _parse_triplet(value: str) -> tuple[RoleId, Level, InterviewFormat]:
    parts = value.split(":")
    if len(parts) != 3:
        raise DomainError(ErrorCode.INVALID_INPUT, field="selection")
    return (
        _parse_enum(RoleId, parts[0], ErrorCode.UNSUPPORTED_ROLE),
        _parse_enum(Level, parts[1], ErrorCode.UNSUPPORTED_LEVEL),
        _parse_enum(InterviewFormat, parts[2], ErrorCode.UNSUPPORTED_FORMAT),
    )


# ---------------------------------------------------------------------------
# Ответы на ошибки (§3.2: конкретная причина и одно доступное действие)
# ---------------------------------------------------------------------------


async def _enqueue_text(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    text: str,
    *,
    suffix: str,
    session_id: uuid.UUID | None = None,
) -> None:
    await outbox_repo.enqueue(
        conn,
        unique_key=f"ui:{item['update_id']}:{suffix}",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="text",
        payload=text_payload(user["chat_id"], text),
        session_id=session_id,
        is_required=True,
    )


async def _respond_domain_error(
    conn: asyncpg.Connection,
    deps: WorkerDeps,
    item: asyncpg.Record,
    user: asyncpg.Record,
    err: DomainError,
) -> None:
    """Отправляет сообщение доменной ошибки (§3.2: конкретная причина и одно
    доступное действие). Вызывается воркером в НОВОЙ транзакции после отката
    частичных эффектов."""
    await _enqueue_text(conn, deps, item, user, err.message, suffix=f"err-{err.code.value}")
    if err.code is ErrorCode.CONSENT_REQUIRED:
        await ui_flow.render_consent(
            conn, deps.config, user=user, update_id=item["update_id"]
        )
    elif err.code is ErrorCode.STALE_ACTION:
        await ui_flow.render_current_screen(
            conn, deps.config, user=user, update_id=item["update_id"]
        )


async def notify_domain_error(
    conn: asyncpg.Connection, deps: WorkerDeps, item: asyncpg.Record, err: DomainError
) -> None:
    """Точка входа воркера для уведомления об ошибке после отката транзакции."""
    payload = _payload(item)
    if payload is None:
        return
    telegram_user_id: int | None = None
    if item["kind"] == "message":
        telegram_user_id = (payload.get("message", {}).get("from") or {}).get("id")
    elif item["kind"] == "callback_query":
        telegram_user_id = (payload.get("callback_query", {}).get("from") or {}).get("id")
    if not isinstance(telegram_user_id, int):
        return
    user = await users_repo.lock_by_telegram_id(conn, telegram_user_id)
    if user is None:
        return
    await _respond_domain_error(conn, deps, item, user, err)
