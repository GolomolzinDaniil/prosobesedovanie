"""Генерация непрозрачных UI-токенов (§3.1, §1.4).

16 случайных байтов в base64url без padding = 22 ASCII-символа.
callback_data ограничена 64 байтами Telegram; токен непрозрачен и не содержит
идентификаторов сущностей.
"""

from __future__ import annotations

import base64
import secrets

from app.domain.errors import DomainError
from contracts.models import UI_TOKEN_LENGTH, ErrorCode

CALLBACK_DATA_MAX_BYTES = 64


def generate_ui_token() -> str:
    raw = secrets.token_bytes(16)
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert len(token) == UI_TOKEN_LENGTH
    return token


def build_callback_data(action: str, token: str) -> str:
    """callback_data = '<action>:<token>'; жёсткий предел 64 байта (§1.4)."""
    data = f"{action}:{token}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            message="Внутренняя ошибка формата кнопки.",
        )
    return data


def parse_callback_data(data: str) -> tuple[str, str]:
    action, _, token = data.partition(":")
    if not action or not token:
        raise DomainError(ErrorCode.STALE_ACTION)
    return action, token
