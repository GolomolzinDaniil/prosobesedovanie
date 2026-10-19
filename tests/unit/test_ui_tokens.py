"""UI-токены: 16 случайных байт base64url без padding = 22 символа (§3.1)."""

from __future__ import annotations

import base64

import pytest

from app.domain.errors import DomainError
from app.services.ui_tokens import build_callback_data, generate_ui_token, parse_callback_data


def test_token_length_and_charset() -> None:
    token = generate_ui_token()
    assert len(token) == 22
    assert token.isascii()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(token) <= allowed


def test_token_decodes_to_16_random_bytes() -> None:
    token = generate_ui_token()
    raw = base64.urlsafe_b64decode(token + "==")
    assert len(raw) == 16


def test_tokens_are_unique() -> None:
    tokens = {generate_ui_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_callback_data_within_64_bytes() -> None:
    data = build_callback_data("report_rating_not_useful", generate_ui_token())
    assert len(data.encode("utf-8")) <= 64


def test_callback_data_too_long_rejected() -> None:
    with pytest.raises(DomainError):
        build_callback_data("a" * 60, generate_ui_token())


def test_callback_roundtrip() -> None:
    token = generate_ui_token()
    data = build_callback_data("skip_turn", token)
    action, parsed_token = parse_callback_data(data)
    assert action == "skip_turn"
    assert parsed_token == token


def test_callback_without_token_is_stale() -> None:
    with pytest.raises(DomainError) as exc_info:
        parse_callback_data("malformed")
    assert exc_info.value.code.value == "STALE_ACTION"
