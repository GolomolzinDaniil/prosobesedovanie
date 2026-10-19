"""Классификация Telegram Update для webhook (§3.1)."""

from __future__ import annotations

import pytest

from app.telegram.updates import (
    InvalidUpdateError,
    classify_update,
    extract_telegram_user_id,
)


def message_update(update_id: int = 1, **extra) -> dict:
    msg = {
        "message_id": 10,
        "date": 1760000000,
        "chat": {"id": 555, "type": "private"},
        "from": {"id": 555},
        "text": "/start",
    }
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


def test_message_supported() -> None:
    assert classify_update(message_update()) == ("message", True)


def test_callback_query_supported() -> None:
    update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 555},
            "chat_instance": "-100",
            "data": "select_role:token22chars00000000",
        },
    }
    assert classify_update(update) == ("callback_query", True)


def test_pre_checkout_follows_billing_flag() -> None:
    update = {
        "update_id": 3,
        "pre_checkout_query": {
            "id": "pcq1",
            "from": {"id": 555},
            "currency": "XTR",
            "total_amount": 75,
            "invoice_payload": "ord_1",
        },
    }
    # оплата выключена -> признан обработанным без вызова AI
    assert classify_update(update, billing_enabled=False) == ("pre_checkout_query", False)
    # коммерческий этап -> быстрый путь API
    assert classify_update(update, billing_enabled=True) == ("pre_checkout_query", True)


@pytest.mark.parametrize(
    "kind_field",
    ["edited_message", "channel_post", "edited_channel_post", "inline_query", "poll",
     "my_chat_member", "chat_join_request", "shipping_query", "message_reaction"],
)
def test_unsupported_kinds_acked_without_ai(kind_field: str) -> None:
    kind, supported = classify_update({"update_id": 4, kind_field: {"any": "content"}})
    assert kind == f"unsupported:{kind_field}"
    assert supported is False


def test_update_without_content_fields_is_unsupported_unknown() -> None:
    assert classify_update({"update_id": 5}) == ("unsupported:unknown", False)


@pytest.mark.parametrize(
    "payload",
    [
        {},                                    # нет update_id
        {"update_id": "10", "message": {}},    # update_id строкой
        {"update_id": True},                   # bool не является идентификатором
        {"update_id": -1},                     # неположительный
        {"update_id": 1, "message": {"message_id": 1, "date": 1}},  # нет chat
        {"update_id": 1, "message": {"message_id": 1, "date": 1, "chat": {"id": 1}}},  # нет type
        {
            "update_id": 1,
            "message": {"message_id": "x", "date": 1, "chat": {"id": 1, "type": "private"}},
        },
        {
            "update_id": 1,
            "message": {
                "message_id": 1, "date": 1,
                "chat": {"id": 1, "type": "private"}, "text": 5,
            },
        },
        [1, 2, 3],                             # не объект
        "строка",
    ],
)
def test_invalid_structure_raises(payload) -> None:
    with pytest.raises(InvalidUpdateError):
        classify_update(payload)


def test_voice_negative_duration_rejected() -> None:
    update = message_update(voice={"file_id": "AgADBA", "duration": -5})
    with pytest.raises(InvalidUpdateError):
        classify_update(update)


def test_voice_structure_ok() -> None:
    update = message_update(voice={"file_id": "AgADBA", "duration": 30})
    assert classify_update(update)[0] == "message"


def test_extract_telegram_user_id() -> None:
    assert extract_telegram_user_id(message_update(), "message") == 555
    cb = {
        "update_id": 2,
        "callback_query": {"id": "cb1", "from": {"id": 777}, "chat_instance": "x"},
    }
    assert extract_telegram_user_id(cb, "callback_query") == 777
    assert extract_telegram_user_id(message_update(), "unsupported:poll") is None


def test_group_chat_message_still_message_kind() -> None:
    """Проверка личного чата — задача домена (часть 2), не webhook:
    в группе бот предложит открыть личный чат и не будет сохранять ответы (§1.3)."""
    update = message_update()
    update["message"]["chat"] = {"id": -100555, "type": "group"}
    kind, supported = classify_update(update)
    assert (kind, supported) == ("message", True)
