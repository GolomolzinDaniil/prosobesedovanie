"""Конструкторы payload sendMessage и reply_markup (§3.1, §3.7).

- ForceReply используется только у вопроса; ForceReply и InlineKeyboardMarkup
  нельзя задавать одновременно одним reply_markup [S1] — кнопки управления
  отправляются отдельным сообщением;
- callback_data = "<action>:<ui_token>", непрозрачный токен 22 ASCII-символа,
  предел 64 байта;
- сообщения формируются сервером в plain text: parse_mode не используется.
"""

from __future__ import annotations

from typing import Any

from app.services.ui_tokens import build_callback_data

Button = tuple[str, str]  # (текст кнопки, callback_data)


def force_reply_payload(chat_id: int, text: str) -> dict[str, Any]:
    """Сообщение вопроса с ForceReply (selective — только личный чат)."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"force_reply": True, "selective": True},
    }


def inline_keyboard(rows: list[list[Button]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def buttons_payload(chat_id: int, text: str, rows: list[list[Button]]) -> dict[str, Any]:
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": text,
        "reply_markup": inline_keyboard(rows),
    }


def text_payload(chat_id: int, text: str) -> dict[str, Any]:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text}


def reply_payload(chat_id: int, text: str, message_id: int) -> dict[str, Any]:
    """Текстовое сообщение-ответ на конкретное сообщение (например, вопрос)."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": text,
        "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True},
    }


def action_button(action: str, token: str, label: str) -> Button:
    return (label, build_callback_data(action, token))


def optional(reply_markup: dict[str, Any] | None) -> dict[str, Any]:
    return {"reply_markup": reply_markup} if reply_markup else {}
