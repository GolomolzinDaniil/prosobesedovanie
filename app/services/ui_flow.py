"""Потоки входа: согласие и выбор параметров (§3.7, задача T07).

Навигация: /start -> согласие -> роль -> уровень -> формат -> сводка ->
интервью. Выбор выполняется отдельными сообщениями с inline-кнопками и
возможностью вернуться; возврат к выбору до старта не расходует попытку.
UI-селекция выполняется одноразовыми токенами ui_tokens (30 минут); значения
кнопок непрозрачны — разрешённое значение хранится на сервере (§3.1).

Рендеры идемпотентны: повторная обработка того же update_id не создаёт
вторую копию экрана (unique_key outbox привязан к update_id).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import asyncpg

from app.services.ui_tokens import generate_ui_token
from app.storage import outbox_repo, ui_tokens_repo, users_repo
from app.telegram import messages
from app.telegram.markup import Button, buttons_payload, text_payload
from contracts.models import InterviewFormat, Level, RoleId

SELECTION_TTL_MINUTES = 30


def _selection_ttl(config) -> timedelta:
    return timedelta(minutes=config.settings.ui_tokens.parameter_selection_minutes)


async def _already_rendered(conn: asyncpg.Connection, unique_key: str) -> bool:
    return await outbox_repo.get_by_unique_key(conn, unique_key) is not None


async def _issue_token(
    conn: asyncpg.Connection,
    config,
    *,
    user_id: uuid.UUID,
    action: str,
    allowed_value: str | None = None,
    session_id: uuid.UUID | None = None,
    expected_version: int | None = None,
    ttl: timedelta | None = None,
) -> str:
    token = generate_ui_token()
    await ui_tokens_repo.issue(
        conn,
        token=token,
        user_id=user_id,
        action=action,
        ttl=ttl or _selection_ttl(config),
        allowed_value=allowed_value,
        session_id=session_id,
        expected_version=expected_version,
    )
    return token


async def _enqueue_screen(
    conn: asyncpg.Connection,
    *,
    user,
    update_id: int,
    screen: str,
    rows: list[dict],
) -> None:
    """Отправляет экран (1..N сообщений) идемпотентно относительно update_id."""
    for index, payload in enumerate(rows):
        kind = "buttons" if payload.get("reply_markup") else "text"
        await outbox_repo.enqueue(
            conn,
            unique_key=f"ui:{update_id}:{screen}:{index}",
            user_id=user["id"],
            chat_id=user["chat_id"],
            kind=kind,
            payload=payload,
            is_required=True,
        )


async def render_consent(
    conn: asyncpg.Connection, config, *, user, update_id: int
) -> None:
    """Вход: краткое объяснение длительности + уведомление об AI-обработке,
    сроках хранения и способе удаления; кнопки согласия (§3.7)."""
    if await _already_rendered(conn, f"ui:{update_id}:consent:0"):
        return
    await ui_tokens_repo.revoke_unconsumed(conn, user["id"])
    accept_token = await _issue_token(
        conn, config, user_id=user["id"], action="consent_accept"
    )
    decline_token = await _issue_token(
        conn, config, user_id=user["id"], action="consent_decline"
    )
    notice = messages.CONSENT_NOTICE.format(
        consent_version=messages.CONSENT_DOCUMENT_VERSION
    )
    keyboard = [
        [("Принимаю и продолжаю", f"consent_accept:{accept_token}")],
        [("Не принимаю", f"consent_decline:{decline_token}")],
    ]
    await users_repo.set_ui_state(conn, user["id"], "awaiting_consent")
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="consent",
        rows=[
            text_payload(user["chat_id"], messages.GREETING),
            buttons_payload(user["chat_id"], notice, keyboard),
        ],
    )


async def render_role_selection(
    conn: asyncpg.Connection, config, *, user, update_id: int
) -> None:
    if await _already_rendered(conn, f"ui:{update_id}:role:0"):
        return
    await ui_tokens_repo.revoke_unconsumed(conn, user["id"])
    rows: list[list[Button]] = []
    for role in config.settings.roles:
        token = await _issue_token(
            conn, config, user_id=user["id"], action="select_role", allowed_value=role.value
        )
        rows.append([(messages.ROLE_LABELS[role.value], f"select_role:{token}")])
    await users_repo.set_ui_state(conn, user["id"], "selecting_role")
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="role",
        rows=[buttons_payload(user["chat_id"], messages.SELECT_ROLE, rows)],
    )


async def render_level_selection(
    conn: asyncpg.Connection, config, *, user, update_id: int, role: RoleId
) -> None:
    if await _already_rendered(conn, f"ui:{update_id}:level:0"):
        return
    await ui_tokens_repo.revoke_unconsumed(conn, user["id"])
    rows: list[list[Button]] = []
    for level in config.settings.levels:
        token = await _issue_token(
            conn,
            config,
            user_id=user["id"],
            action="select_level",
            allowed_value=f"{role.value}:{level.value}",
        )
        rows.append([(messages.LEVEL_LABELS[level.value], f"select_level:{token}")])
    back_token = await _issue_token(conn, config, user_id=user["id"], action="back_to_role")
    rows.append([(messages.BACK, f"back_to_role:{back_token}")])
    await users_repo.set_ui_state(conn, user["id"], f"selecting_level:{role.value}")
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="level",
        rows=[buttons_payload(user["chat_id"], messages.SELECT_LEVEL, rows)],
    )


async def render_format_selection(
    conn: asyncpg.Connection,
    config,
    *,
    user,
    update_id: int,
    role: RoleId,
    level: Level,
) -> None:
    if await _already_rendered(conn, f"ui:{update_id}:format:0"):
        return
    await ui_tokens_repo.revoke_unconsumed(conn, user["id"])
    rows: list[list[Button]] = []
    for fmt in config.settings.formats:
        if fmt is InterviewFormat.voice and not config.settings.flags.voice_enabled:
            # выключение voice переводит сборку в промежуточный прототип (§3.6)
            continue
        token = await _issue_token(
            conn,
            config,
            user_id=user["id"],
            action="select_format",
            allowed_value=f"{role.value}:{level.value}:{fmt.value}",
        )
        rows.append([(messages.FORMAT_LABELS[fmt.value], f"select_format:{token}")])
    back_token = await _issue_token(
        conn,
        config,
        user_id=user["id"],
        action="back_to_level",
        allowed_value=role.value,
    )
    rows.append([(messages.BACK, f"back_to_level:{back_token}")])
    await users_repo.set_ui_state(
        conn, user["id"], f"selecting_format:{role.value}:{level.value}"
    )
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="format",
        rows=[buttons_payload(user["chat_id"], messages.SELECT_FORMAT, rows)],
    )


async def render_summary(
    conn: asyncpg.Connection,
    config,
    *,
    user,
    update_id: int,
    role: RoleId,
    level: Level,
    fmt: InterviewFormat,
) -> None:
    """Сводка фиксирует все параметры; «Начать» расходует резерв, не попытку."""
    if await _already_rendered(conn, f"ui:{update_id}:summary:0"):
        return
    await ui_tokens_repo.revoke_unconsumed(conn, user["id"])
    summary = messages.SUMMARY.format(
        role=messages.ROLE_LABELS[role.value],
        level=messages.LEVEL_LABELS[level.value],
        fmt=messages.FORMAT_LABELS[fmt.value],
    )
    start_token = await _issue_token(
        conn,
        config,
        user_id=user["id"],
        action="start_session",
        allowed_value=f"{role.value}:{level.value}:{fmt.value}",
    )
    back_token = await _issue_token(
        conn,
        config,
        user_id=user["id"],
        action="back_to_format",
        allowed_value=f"{role.value}:{level.value}",
    )
    keyboard = [
        [(messages.START_LABEL, f"start_session:{start_token}")],
        [(messages.BACK, f"back_to_format:{back_token}")],
    ]
    await users_repo.set_ui_state(
        conn, user["id"], f"awaiting_summary:{role.value}:{level.value}:{fmt.value}"
    )
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="summary",
        rows=[buttons_payload(user["chat_id"], summary, keyboard)],
    )


async def render_delete_confirmation(
    conn: asyncpg.Connection, config, *, user, update_id: int
) -> None:
    """/delete: явное предупреждение + одноразовый токен на 5 минут (§3.1, §3.7)."""
    if await _already_rendered(conn, f"ui:{update_id}:delete:0"):
        return
    ttl = timedelta(minutes=config.settings.ui_tokens.delete_confirm_minutes)
    confirm_token = await _issue_token(
        conn, config, user_id=user["id"], action="confirm_delete", ttl=ttl
    )
    decline_token = await _issue_token(
        conn, config, user_id=user["id"], action="delete_decline", ttl=ttl
    )
    keyboard = [
        [(messages.DELETE_CONFIRM_LABEL, f"confirm_delete:{confirm_token}")],
        [(messages.DELETE_DECLINE_LABEL, f"delete_decline:{decline_token}")],
    ]
    await _enqueue_screen(
        conn,
        user=user,
        update_id=update_id,
        screen="delete",
        rows=[buttons_payload(user["chat_id"], messages.DELETE_WARNING, keyboard)],
    )


async def render_current_screen(
    conn: asyncpg.Connection, config, *, user, update_id: int
) -> None:
    """Показать актуальный экран после устаревания токена (§3.1).

    Незавершённый выбор сохраняется в ui_state (роль/уровень/формат), поэтому
    экран восстанавливается точно; для активной сессии экраны не перерисовываются.
    """
    from contracts.models import InterviewFormat, Level, RoleId

    ui_state = user["ui_state"] or "new"
    base, _, value = ui_state.partition(":")
    if base in ("new", "awaiting_consent", "consent_declined"):
        await render_consent(conn, config, user=user, update_id=update_id)
        return
    if base == "selecting_role":
        await render_role_selection(conn, config, user=user, update_id=update_id)
        return
    if base == "selecting_level" and value:
        try:
            role = RoleId(value)
        except ValueError:
            role = config.settings.roles[0]
        await render_level_selection(conn, config, user=user, update_id=update_id, role=role)
        return
    if base == "selecting_format":
        parts = value.split(":")
        if len(parts) == 2:
            try:
                await render_format_selection(
                    conn,
                    config,
                    user=user,
                    update_id=update_id,
                    role=RoleId(parts[0]),
                    level=Level(parts[1]),
                )
                return
            except ValueError:
                pass
    if base == "awaiting_summary":
        parts = value.split(":")
        if len(parts) == 3:
            try:
                await render_summary(
                    conn,
                    config,
                    user=user,
                    update_id=update_id,
                    role=RoleId(parts[0]),
                    level=Level(parts[1]),
                    fmt=InterviewFormat(parts[2]),
                )
                return
            except ValueError:
                pass
    # состояние не восстановимо (в т.ч. in_session) — начинаем с роли
    if base != "in_session":
        await render_role_selection(conn, config, user=user, update_id=update_id)
