"""Нормализация и граничные длины ответа (§3.3, AT09: 0/1/4000/4001)."""

from __future__ import annotations

import pytest

from app.domain.answers import normalize_answer_text, validate_answer_text
from app.domain.errors import DomainError
from contracts.models import ANSWER_MAX_CHARS, ErrorCode


def test_nfc_normalization() -> None:
    # «ё» decomposed (е + combining diaeresis) приводится к NFC
    decomposed = "e\u0308 jo"
    assert normalize_answer_text(decomposed) == "\u00eb jo"


def test_crlf_and_cr_become_lf() -> None:
    assert normalize_answer_text("a\r\nb\rc") == "a\nb\nc"


def test_outer_whitespace_and_blank_lines_stripped() -> None:
    assert normalize_answer_text("\n\n  привет  \n\n") == "привет"


def test_internal_indentation_preserved() -> None:
    code = "def f():\n\treturn 1\n    # отступ сохранён"
    assert normalize_answer_text(f"  {code}  ") == code


def test_tab_and_lf_allowed() -> None:
    assert validate_answer_text("строка\tс табами\nи переносами") == (
        "строка\tс табами\nи переносами"
    )


@pytest.mark.parametrize("bad", ["\x00", "a\x00b", "\x1f", "a\x7fb", "\x0b"])
def test_control_chars_rejected(bad: str) -> None:
    with pytest.raises(DomainError) as exc_info:
        validate_answer_text(bad)
    assert exc_info.value.code is ErrorCode.INVALID_INPUT
    assert exc_info.value.field == "text"


def test_nul_byte_rejected_explicitly() -> None:
    with pytest.raises(DomainError):
        normalize_answer_text("ответ\x00со скрытым байтом")


def test_empty_answer_rejected() -> None:
    for empty in ("", "   ", "\n\n", "\t"):
        with pytest.raises(DomainError) as exc_info:
            validate_answer_text(empty)
        assert exc_info.value.code is ErrorCode.EMPTY_ANSWER
        assert exc_info.value.message == "Напишите ответ от 1 до 4000 символов."


def test_boundary_lengths_1_and_4000_accepted() -> None:
    assert validate_answer_text("а") == "а"
    text = "а" * ANSWER_MAX_CHARS
    assert validate_answer_text(text) == text
    assert len(validate_answer_text(text)) == 4000


def test_4001_chars_rejected_not_truncated() -> None:
    text = "а" * (ANSWER_MAX_CHARS + 1)
    with pytest.raises(DomainError) as exc_info:
        validate_answer_text(text)
    err = exc_info.value
    assert err.code is ErrorCode.ANSWER_TOO_LONG
    assert err.message == "Ответ должен содержать не более 4000 символов."
    assert err.retryable is False


def test_length_measured_in_codepoints_after_normalization() -> None:
    # astral-символ считается одной кодовой точкой
    emoji = "😀" * 4000
    assert validate_answer_text(emoji) == emoji
    with pytest.raises(DomainError):
        validate_answer_text(emoji + "😀")
    # окружающие пробелы не входят в длину после нормализации
    padded = "  " + "а" * 4000 + "\n\n"
    assert len(validate_answer_text(padded)) == 4000


def test_whitespace_only_4001_chars_is_empty_answer() -> None:
    # после нормализации пусто -> EMPTY_ANSWER, а не TOO_LONG
    with pytest.raises(DomainError) as exc_info:
        validate_answer_text(" " * 4001)
    assert exc_info.value.code is ErrorCode.EMPTY_ANSWER
