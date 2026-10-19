"""Разбор и классификация Telegram Update для webhook (§3.1).

Правила:
- принимать только JSON не более 1 MiB (проверяет route-слой);
- неверная структура -> 400 (InvalidUpdateError);
- поддерживаемые виды: message, callback_query; после включения оплаты —
  pre_checkout_query (быстрый путь API, вне очереди LLM);
- неподдерживаемый вид признаётся обработанным без вызова AI (статус skipped);
- payload сохраняется в inbox как есть (raw JSON) — доменная обработка
  в worker; отредактированные сообщения не изменяют принятые ответы (§2.4).
"""

from __future__ import annotations

from typing import Any

# Порядок полей Update в Bot API (определяет вид обновления)
KNOWN_UPDATE_KINDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message_reaction",
    "message_reaction_count",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "purchased_paid_media",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
    "chat_boost",
    "removed_chat_boost",
)

# Виды, которые worker умеет обрабатывать (allowed_updates, §3.1)
SUPPORTED_UPDATE_KINDS: frozenset[str] = frozenset({"message", "callback_query"})
# Быстрый путь API коммерческого этапа (billing_enabled=false -> unsupported)
PRE_CHECKOUT_KIND = "pre_checkout_query"

UNSUPPORTED_KIND_LABEL = "unsupported"


class InvalidUpdateError(Exception):
    """Тело не является корректным Telegram Update -> HTTP 400."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _require_int(data: Any, field: str, path: str) -> int:
    value = data.get(field)
    # bool — подтип int в Python; для Telegram-идентификаторов недопустим
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidUpdateError(f"{path}.{field} обязан быть целым числом")
    return value


def _require_str(data: Any, field: str, path: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise InvalidUpdateError(f"{path}.{field} обязан быть строкой")
    return value


def _require_dict(data: Any, field: str, path: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise InvalidUpdateError(f"{path}.{field} обязан быть объектом")
    return value


def _validate_message(msg: dict[str, Any], path: str) -> None:
    """Минимальная структурная проверка Message: обязательные поля Bot API.

    Необязательные поля (text, voice, photo, document, ...) не валидируются
    глубоко: содержательные отказы (например, VOICE_FORMAT_UNSUPPORTED для
    документов) выполняет домен worker-а, а не webhook.
    """
    _require_int(msg, "message_id", path)
    _require_int(msg, "date", path)
    chat = _require_dict(msg, "chat", f"{path}.chat")
    _require_int(chat, "id", f"{path}.chat")
    _require_str(chat, "type", f"{path}.chat")
    if "from" in msg:
        sender = _require_dict(msg, "from", f"{path}.from")
        _require_int(sender, "id", f"{path}.from")
    if "text" in msg and not isinstance(msg["text"], str):
        raise InvalidUpdateError(f"{path}.text обязан быть строкой")
    if "voice" in msg:
        voice = _require_dict(msg, "voice", f"{path}.voice")
        _require_str(voice, "file_id", f"{path}.voice")
        duration = _require_int(voice, "duration", f"{path}.voice")
        if duration < 0:
            raise InvalidUpdateError(f"{path}.voice.duration не может быть отрицательным")
    if "reply_to_message" in msg:
        reply = _require_dict(msg, "reply_to_message", f"{path}.reply_to_message")
        _require_int(reply, "message_id", f"{path}.reply_to_message")


def _validate_callback_query(cb: dict[str, Any], path: str) -> None:
    _require_str(cb, "id", path)
    sender = _require_dict(cb, "from", f"{path}.from")
    _require_int(sender, "id", f"{path}.from")
    _require_str(cb, "chat_instance", path)
    if "data" in cb and not isinstance(cb["data"], str):
        raise InvalidUpdateError(f"{path}.data обязан быть строкой")
    if "message" in cb and cb["message"] is not None:
        _validate_message(cb["message"], f"{path}.message")


def _validate_pre_checkout_query(pcq: dict[str, Any], path: str) -> None:
    _require_str(pcq, "id", path)
    sender = _require_dict(pcq, "from", f"{path}.from")
    _require_int(sender, "id", f"{path}.from")
    _require_str(pcq, "currency", path)
    _require_int(pcq, "total_amount", path)
    _require_str(pcq, "invoice_payload", path)


def extract_update_id(data: Any) -> int:
    if not isinstance(data, dict):
        raise InvalidUpdateError("update обязан быть JSON-объектом")
    update_id = _require_int(data, "update_id", "update")
    if update_id <= 0:
        raise InvalidUpdateError("update_id обязан быть положительным")
    return update_id


def classify_update(data: Any, *, billing_enabled: bool = False) -> tuple[str, bool]:
    """Валидирует структуру и классифицирует обновление.

    Возвращает (kind, supported):
    - kind — вид обновления для inbox ('message', 'callback_query',
      'pre_checkout_query', либо 'unsupported:<field>' / 'unsupported:unknown');
    - supported — нужно ли доменное обслуживание (pending), либо обновление
      признаётся обработанным без вызова AI (skipped).

    Бросает InvalidUpdateError при неверной структуре -> HTTP 400.
    """
    if not isinstance(data, dict):
        raise InvalidUpdateError("update обязан быть JSON-объектом")
    extract_update_id(data)

    for field in KNOWN_UPDATE_KINDS:
        if field not in data or data[field] is None:
            continue
        if field == "message":
            _validate_message(data[field], "message")
            return field, True
        if field == "callback_query":
            _validate_callback_query(data[field], "callback_query")
            return field, True
        if field == PRE_CHECKOUT_KIND:
            _validate_pre_checkout_query(data[field], "pre_checkout_query")
            # Быстрый путь API включается только с коммерческим этапом (§2.1)
            return field, billing_enabled
        # Прочие известные виды: структура не валидируется глубоко,
        # обновление признаётся обработанным без вызова AI.
        return f"unsupported:{field}", False
    return "unsupported:unknown", False


def extract_telegram_user_id(data: dict[str, Any], kind: str) -> int | None:
    """from.id для supported-видов; None для неизвестного пользователя (inbox
    допускает nullable user_id, §2.3). Используется worker-ом при разрешении
    профиля; webhook не выполняет lookup."""
    try:
        if kind == "message":
            sender = data.get("message", {}).get("from")
        elif kind == "callback_query":
            sender = data.get("callback_query", {}).get("from")
        elif kind == PRE_CHECKOUT_KIND:
            sender = data.get(PRE_CHECKOUT_KIND, {}).get("from")
        else:
            return None
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            return int(sender["id"])
    except AttributeError:  # pragma: no cover - защита от частично валидных данных
        return None
    return None
