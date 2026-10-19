"""Нормализация и проверка длины ответа пользователя (§3.3, первый абзац).

Правила:
- Unicode NFC; CRLF/CR -> LF;
- убрать внешние пробелы и пустые строки; внутренние отступы и переносы
  сохраняются (фрагменты кода);
- нулевой байт и двоичные управляющие символы отклоняются; табуляция и LF
  допустимы;
- длина проверяется ПОСЛЕ нормализации в кодовых точках Unicode;
- пустой после нормализации ответ отклоняется (EMPTY_ANSWER);
- слишком длинный НЕ обрезается (ANSWER_TOO_LONG);
- код, ссылки и инструкции в ответе не выполняются (обработка текста
  доменом/провайдером как данные — см. prompts/user-envelope.txt).
"""

from __future__ import annotations

import unicodedata

from app.domain.errors import DomainError
from contracts.models import ANSWER_MAX_CHARS, ANSWER_MIN_CHARS, ErrorCode


def _reject_control_chars(text: str) -> None:
    for ch in text:
        if ch in ("\t", "\n"):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf") and ch != "\ufeff":
            # Нулевой байт и двоичные управляющие символы отклоняются.
            # Cf (форматные, напр. невидимые разделители) также отклоняются,
            # кроме BOM, который безопасен после нормализации.
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                message="Ответ содержит недопустимые управляющие символы. "
                f"Напишите ответ от {ANSWER_MIN_CHARS} до {ANSWER_MAX_CHARS} символов.",
                field="text",
            )


def normalize_answer_text(raw: str) -> str:
    """NFC -> LF -> отклонение управляющих символов -> strip внешних пробелов/пустых строк."""
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    _reject_control_chars(text)
    return text.strip()


def validate_answer_text(raw: str) -> str:
    """Возвращает нормализованный ответ либо бросает доменную ошибку.

    Длина измеряется в кодовых точках Unicode после нормализации.
    Ответ никогда не обрезается молча.
    """
    normalized = normalize_answer_text(raw)
    length = len(normalized)
    if length < ANSWER_MIN_CHARS:
        raise DomainError(ErrorCode.EMPTY_ANSWER, field="text")
    if length > ANSWER_MAX_CHARS:
        raise DomainError(ErrorCode.ANSWER_TOO_LONG, field="text")
    return normalized
