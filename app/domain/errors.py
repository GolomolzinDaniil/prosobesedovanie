"""Единая доменная ошибка и реестр кодов (§3.2).

message предназначен пользователю; код стабилен для обработчиков; retryable
означает возможность повторить после устранения причины. При RATE_LIMITED и
временном отказе указывается retry_after_seconds. Контекст провайдера,
трассировка и секреты в сообщение не попадают.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import ValidationError

from contracts.models import ErrorBody, ErrorCode, ErrorResponse

# Реестр: код -> (сообщение пользователю по умолчанию, retryable)
ERROR_REGISTRY: dict[ErrorCode, tuple[str, bool]] = {
    ErrorCode.EMPTY_ANSWER: ("Напишите ответ от 1 до 4000 символов.", False),
    ErrorCode.ANSWER_TOO_LONG: ("Ответ должен содержать не более 4000 символов.", False),
    ErrorCode.ACTIVE_SESSION: ("У вас есть активная тренировка.", False),
    ErrorCode.NO_CREDIT: ("Нет доступной попытки.", False),
    ErrorCode.BUSY: ("Ответ сохранён. Подождите следующий вопрос.", False),
    ErrorCode.STALE_ACTION: ("Кнопка устарела. Используйте актуальное управление.", False),
    ErrorCode.REPLY_REQUIRED: ("Ответьте на последний вопрос.", False),
    ErrorCode.DELIVERY_FAILED: ("Доставить результат не удалось.", False),
    ErrorCode.CONSENT_REQUIRED: ("Подтвердите условия обработки данных.", False),
    ErrorCode.NOT_FOUND: ("Сессия недоступна.", False),
    ErrorCode.NO_ANSWERS: ("Для разбора нужен хотя бы один ответ.", False),
    ErrorCode.DELETION_IN_PROGRESS: (
        "Удаление профиля принято. Новые действия недоступны до завершения операции.",
        False,
    ),
    ErrorCode.VOICE_TOO_LARGE: ("Запишите до 120 секунд и 10 MiB.", False),
    ErrorCode.MEDIA_STORAGE_FULL: (
        "Сейчас не могу принять голосовую запись. Ответьте текстом или попробуйте позже.",
        True,
    ),
    ErrorCode.VOICE_TOO_LONG: ("Запишите до 120 секунд и 10 MiB.", False),
    ErrorCode.VOICE_FORMAT_UNSUPPORTED: ("Отправьте голосовое сообщение Telegram.", False),
    ErrorCode.TRANSCRIPTION_FAILED: (
        "Не удалось распознать. Запишите снова или ответьте текстом.",
        False,
    ),
    ErrorCode.AI_UNAVAILABLE: ("Интервью продолжается с ограниченной оценкой.", False),
    ErrorCode.AI_INVALID_OUTPUT: ("Интервью продолжается с ограниченной оценкой.", False),
    ErrorCode.BUDGET_EXCEEDED: ("Подготовим разбор уже полученных ответов.", False),
    ErrorCode.REPORT_NOT_READY: ("Отчёт готовится.", True),
    ErrorCode.REPORT_EXPIRED: ("Срок хранения отчёта истёк.", False),
    ErrorCode.PAYMENT_MISMATCH: ("Оплата проверяется.", False),
    ErrorCode.REFUND_PENDING: ("Возврат выполняется.", False),
    ErrorCode.RATE_LIMITED: ("Попробуйте через указанное время.", True),
    ErrorCode.INVALID_INPUT: ("Запрос содержит ошибку.", False),
    ErrorCode.UNSUPPORTED_ROLE: ("Такая специализация не поддерживается.", False),
    ErrorCode.UNSUPPORTED_LEVEL: (
        "Такой уровень не поддерживается: выберите intern или junior.",
        False,
    ),
    ErrorCode.UNSUPPORTED_FORMAT: (
        "Такой формат не поддерживается: выберите текст или голос.",
        False,
    ),
    ErrorCode.INVALID_UPDATE: ("Тело запроса не является корректным Telegram Update.", False),
    ErrorCode.FORBIDDEN: ("Доступ запрещён.", False),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: ("Поддерживается только application/json.", False),
    ErrorCode.PAYLOAD_TOO_LARGE: ("Тело запроса превышает 1 MiB.", False),
    ErrorCode.SERVICE_UNAVAILABLE: ("Сервис временно недоступен. Повторите позже.", True),
    ErrorCode.INTERNAL_ERROR: ("Внутренняя ошибка. Повторите позже.", True),
}


class DomainError(Exception):
    """Доменная ошибка: стабильный код, пользовательское сообщение, retryable."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str | None = None,
        field: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        if code not in ERROR_REGISTRY:
            raise ValueError(f"Код {code!r} отсутствует в реестре ошибок")
        default_message, retryable = ERROR_REGISTRY[code]
        self.code = code
        self.message = message or default_message
        self.field = field
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        if code is ErrorCode.RATE_LIMITED and retry_after_seconds is None:
            raise ValueError("RATE_LIMITED обязан содержать retry_after_seconds")
        super().__init__(f"{code.value}: {self.message}")

    def body(self) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            field=self.field,
            retry_after_seconds=self.retry_after_seconds,
        )

    def to_response(self, request_id: UUID | None = None) -> ErrorResponse:
        return ErrorResponse(
            ok=False,
            error=self.body(),
            request_id=request_id or uuid4(),
        )


_ENUM_FIELDS = {
    "role_id": ErrorCode.UNSUPPORTED_ROLE,
    "level": ErrorCode.UNSUPPORTED_LEVEL,
    "format": ErrorCode.UNSUPPORTED_FORMAT,
}


def map_all_validation_errors(
    exc: ValidationError, *, ui_selection: bool = False
) -> list[DomainError]:
    """Все ошибки валидации -> список доменных ошибок (порядок pydantic)."""
    result: list[DomainError] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()) if part != "__root__")
        root_field = loc.split(".", 1)[0] if loc else None
        err_type = err.get("type", "")
        code = ErrorCode.INVALID_INPUT
        if (
            ui_selection
            and root_field in _ENUM_FIELDS
            and err_type in ("enum", "value_error", "literal_error")
        ):
            code = _ENUM_FIELDS[root_field]
        message: str | None = None
        if code is ErrorCode.INVALID_INPUT:
            message = f"Некорректное значение поля: {loc or 'unknown'}."
        result.append(DomainError(code, message=message, field=loc or None))
    return result


def map_validation_error(exc: ValidationError, *, ui_selection: bool = False) -> DomainError:
    """Pydantic ValidationError -> первая доменная ошибка (§3.1).

    Внутренняя структурная ошибка возвращается как INVALID_INPUT с именем поля.
    Интерфейс выбора (ui_selection=True) показывает специализированные
    UNSUPPORTED_ROLE/LEVEL/FORMAT, если значение пришло из старой конфигурации.
    """
    mapped = map_all_validation_errors(exc, ui_selection=ui_selection)
    if not mapped:  # pragma: no cover - защита
        return DomainError(ErrorCode.INVALID_INPUT)
    return mapped[0]
