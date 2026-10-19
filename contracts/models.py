"""Нормативные контракты «ПроСобеседование» версии 2.0.

Восстановлены по тексту ТЗ версии 2.0 (разделы 2.4, 3.1–3.5), поскольку архив
спецификаций к документу не приложен. Модели являются единственным источником
форматов: схемы JSON экспортируются из них (scripts/export_schemas.py).

Правила (§3.1):
- неизвестные поля запрещены (extra="forbid");
- строковые перечисления чувствительны к регистру;
- UUID в JSON передаются строками и валидируются как UUID;
- типы не преобразуются автоматически из произвольных строк.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACTS_VERSION = "2.0"

# Максимальные длины из ТЗ (§1.3, §3.1)
ANSWER_MIN_CHARS = 1
ANSWER_MAX_CHARS = 4000
QUESTION_MIN_CHARS = 1
QUESTION_MAX_CHARS = 700
REPORT_MAX_CHARS = 12000
MESSAGE_PART_MAX_CHARS = 3500
UI_TOKEN_LENGTH = 22


class StrictModel(BaseModel):
    """База всех контрактов: неизвестные поля запрещены."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# ---------------------------------------------------------------------------
# Перечисления (реализуются CHECK/enum в БД, §2.3)
# ---------------------------------------------------------------------------


class RoleId(StrEnum):
    python = "python"
    analyst = "analyst"
    qa = "qa"


class Level(StrEnum):
    intern = "intern"
    junior = "junior"


class InterviewFormat(StrEnum):
    text = "text"
    voice = "voice"


class SessionStatus(StrEnum):
    preparing = "preparing"
    delivering_question = "delivering_question"
    awaiting_answer = "awaiting_answer"
    transcribing = "transcribing"
    confirming_transcript = "confirming_transcript"
    evaluating = "evaluating"
    reporting = "reporting"
    completed = "completed"
    cancelled = "cancelled"
    expired = "expired"
    failed = "failed"


TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.completed,
        SessionStatus.cancelled,
        SessionStatus.expired,
        SessionStatus.failed,
    }
)


class TurnKind(StrEnum):
    """Позиция вопроса в плане: основной или уточнение (§2.3)."""

    main = "main"
    follow_up = "follow_up"


class QuestionType(StrEnum):
    """Тип темы вопроса: HR или технический (§3.3). Определяет пару оценок."""

    hr = "hr"
    technical = "technical"


class CreditSource(StrEnum):
    trial = "trial"
    grant = "grant"
    payment = "payment"


class CreditState(StrEnum):
    available = "available"
    reserved = "reserved"
    consumed = "consumed"
    revoked = "revoked"


class JobKind(StrEnum):
    assess = "assess"
    transcribe = "transcribe"
    report = "report"
    tts = "tts"


class JobState(StrEnum):
    pending = "pending"
    leased = "leased"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class OutboxKind(StrEnum):
    question_text = "question_text"
    question_voice = "question_voice"
    report_part = "report_part"
    text = "text"
    buttons = "buttons"


class OutboxState(StrEnum):
    pending = "pending"
    sending = "sending"
    delivered = "delivered"
    failed = "failed"
    cancelled = "cancelled"


class InboxStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    processed = "processed"
    skipped = "skipped"
    failed = "failed"


class PaymentStatus(StrEnum):
    pending = "pending"
    paid = "paid"
    refund_pending = "refund_pending"
    refunded = "refunded"
    expired = "expired"
    reconciliation = "reconciliation"


class SuggestedAction(StrEnum):
    follow_up = "follow_up"
    next_topic = "next_topic"


class ErrorCode(StrEnum):
    """Стабильные коды доменных ошибок (§3.1–3.2). Реестр расширяем, коды стабильны."""

    # ответы и вопросы
    EMPTY_ANSWER = "EMPTY_ANSWER"
    ANSWER_TOO_LONG = "ANSWER_TOO_LONG"
    REPLY_REQUIRED = "REPLY_REQUIRED"
    # сессия и права
    ACTIVE_SESSION = "ACTIVE_SESSION"
    NO_CREDIT = "NO_CREDIT"
    BUSY = "BUSY"
    STALE_ACTION = "STALE_ACTION"
    NO_ANSWERS = "NO_ANSWERS"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    DELETION_IN_PROGRESS = "DELETION_IN_PROGRESS"
    # голос
    VOICE_TOO_LARGE = "VOICE_TOO_LARGE"
    MEDIA_STORAGE_FULL = "MEDIA_STORAGE_FULL"
    VOICE_TOO_LONG = "VOICE_TOO_LONG"
    VOICE_FORMAT_UNSUPPORTED = "VOICE_FORMAT_UNSUPPORTED"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    # AI и бюджет
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_INVALID_OUTPUT = "AI_INVALID_OUTPUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    # отчёт
    REPORT_NOT_READY = "REPORT_NOT_READY"
    REPORT_EXPIRED = "REPORT_EXPIRED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    # оплата (коммерческий этап)
    PAYMENT_MISMATCH = "PAYMENT_MISMATCH"
    REFUND_PENDING = "REFUND_PENDING"
    # ввод и протокол
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_ROLE = "UNSUPPORTED_ROLE"
    UNSUPPORTED_LEVEL = "UNSUPPORTED_LEVEL"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    INVALID_UPDATE = "INVALID_UPDATE"
    FORBIDDEN = "FORBIDDEN"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Доверенный контекст (§3.1): actor_user_id определяется сервером по from.id
# ---------------------------------------------------------------------------


class Context(StrictModel):
    request_id: UUID
    actor_user_id: int = Field(gt=0)
    update_id: int = Field(gt=0)


# ---------------------------------------------------------------------------
# Запросы методов (§3.1)
# ---------------------------------------------------------------------------


class StartSession(StrictModel):
    role_id: RoleId
    level: Level
    format: InterviewFormat


class SubmitAnswer(StrictModel):
    session_id: UUID
    turn_id: UUID
    # Структурных ограничений нет: пустой текст -> EMPTY_ANSWER, 4001 символ ->
    # ANSWER_TOO_LONG возникают как доменные ошибки после нормализации (§3.2, §3.3).
    text: str


class VoiceMessage(StrictModel):
    """Метаданные Telegram voice; не заменяют проверку файла (§3.5)."""

    file_id: str = Field(min_length=1)
    duration: int = Field(gt=0)
    file_size: int | None = Field(default=None, ge=0)
    mime_type: str | None = None
    message_id: int = Field(gt=0)
    session_id: UUID
    turn_id: UUID


class ConfirmTranscript(StrictModel):
    session_id: UUID
    turn_id: UUID
    transcript_version: int = Field(ge=1)
    text: str


class TurnCommand(StrictModel):
    session_id: UUID
    turn_id: UUID


class SessionCommand(StrictModel):
    session_id: UUID


class ConfirmAction(StrictModel):
    token: str = Field(min_length=UI_TOKEN_LENGTH, max_length=UI_TOKEN_LENGTH)


class CreateInvoice(StrictModel):
    product_id: Literal["single", "pack5", "pack10"]


# ---------------------------------------------------------------------------
# Ошибки (§3.2): единый ErrorResponse
# ---------------------------------------------------------------------------


class ErrorBody(StrictModel):
    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    field: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)


class ErrorResponse(StrictModel):
    ok: Literal[False]
    error: ErrorBody
    request_id: UUID


# ---------------------------------------------------------------------------
# Представления и ответы методов (§3.1)
# ---------------------------------------------------------------------------


class TurnView(StrictModel):
    turn_id: UUID
    ordinal: int = Field(ge=1)
    kind: TurnKind
    main_index: int | None = Field(default=None, ge=1, le=6)
    topic: str
    question_text: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    difficulty: int | None = Field(default=None, ge=1, le=3)
    delivered_message_id: int | None = None


class SessionView(StrictModel):
    session_id: UUID
    role_id: RoleId
    level: Level
    format: InterviewFormat
    status: SessionStatus
    version: int = Field(ge=0)
    current_turn: TurnView | None = None
    main_total: int = Field(ge=0, le=6)
    answered_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    follow_ups_used: int = Field(ge=0, le=2)
    created_at: datetime
    expires_at: datetime | None = None


class StartResponse(StrictModel):
    """Общее успешное представление состояния сессии (start/status/cancel/skip/voice)."""

    ok: Literal[True]
    session: SessionView
    job_id: UUID | None = None


class AnswerResponse(StrictModel):
    ok: Literal[True]
    session: SessionView
    job_id: UUID


class ReportResponse(StrictModel):
    ok: Literal[True]
    report: FeedbackReport
    parts: list[str] = Field(min_length=1, max_length=4)
    partial: bool
    degraded: bool


class BalanceResponse(StrictModel):
    ok: Literal[True]
    available: int = Field(ge=0)
    reserved: int = Field(ge=0)
    trial_used: bool


class DeleteResponse(StrictModel):
    ok: Literal[True]
    accepted: bool
    deadline: datetime


class InvoiceResponse(StrictModel):
    ok: Literal[True]
    order_id: UUID
    product_id: Literal["single", "pack5", "pack10"]
    currency: Literal["XTR"]
    amount_xtr: int = Field(gt=0)
    units: int = Field(gt=0)
    payload: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# AI-контракты: оценка ответа (§3.3)
# ---------------------------------------------------------------------------


class TurnScores(StrictModel):
    relevance: int = Field(ge=0, le=4)
    structure: int = Field(ge=0, le=4)
    # Для technical обязателен correctness и evidence=null;
    # для HR наоборот (§3.3). Правило проверяет semantic_validation.
    correctness: int | None = Field(default=None, ge=0, le=4)
    evidence: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def _not_both_topic_scores(self) -> TurnScores:
        if self.correctness is not None and self.evidence is not None:
            raise ValueError("correctness и evidence взаимно исключают друг друга")
        return self


class TurnAssessment(StrictModel):
    turn_id: UUID
    scores: TurnScores
    missing_points: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list, max_length=5)
    explanation: str = Field(min_length=1)
    suggested_action: SuggestedAction
    next_question: str | None = Field(
        default=None, min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS
    )

    @model_validator(mode="after")
    def _follow_up_requires_question(self) -> TurnAssessment:
        if self.suggested_action is SuggestedAction.follow_up and self.next_question is None:
            raise ValueError("next_question обязателен при suggested_action=follow_up")
        return self

    @model_validator(mode="after")
    def _missing_points_not_blank(self) -> TurnAssessment:
        if any(not p.strip() for p in self.missing_points):
            raise ValueError("missing_points не могут быть пустыми строками")
        if any(not q.strip() for q in self.evidence_quotes):
            raise ValueError("evidence_quotes не могут быть пустыми строками")
        return self


class PreviousAssessmentBrief(StrictModel):
    turn_id: UUID
    topic: str = Field(min_length=1)
    scores: TurnScores
    missing_points: list[str] = Field(default_factory=list)
    brief_explanation: str | None = None


class GenerateTurnInput(StrictModel):
    session_id: UUID
    turn_id: UUID
    role_id: RoleId
    level: Level
    kind: TurnKind
    question_type: QuestionType
    topic: str = Field(min_length=1)
    question: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    rubric: dict[str, Any]
    answer: str = Field(min_length=ANSWER_MIN_CHARS, max_length=ANSWER_MAX_CHARS)
    previous_assessments: list[PreviousAssessmentBrief] = Field(
        default_factory=list, max_length=7
    )
    next_allowed_topic: str | None = None
    follow_ups_remaining: int = Field(ge=0, le=2)
    current_difficulty: int = Field(ge=1, le=3)
    difficulty_min: int = Field(ge=1, le=3)
    difficulty_max: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def _bounds(self) -> GenerateTurnInput:
        if self.difficulty_min > self.difficulty_max:
            raise ValueError("difficulty_min не может превышать difficulty_max")
        if not self.difficulty_min <= self.current_difficulty <= self.difficulty_max:
            raise ValueError("current_difficulty вне границ уровня")
        return self


# ---------------------------------------------------------------------------
# AI-контракты: персональный отчёт (§3.4)
# ---------------------------------------------------------------------------


class SnapshotTurn(StrictModel):
    turn_id: UUID
    ordinal: int = Field(ge=1)
    kind: TurnKind
    question_type: QuestionType
    main_index: int | None = Field(default=None, ge=1, le=6)
    topic: str = Field(min_length=1)
    question: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    answer_text: str | None = Field(
        default=None, min_length=ANSWER_MIN_CHARS, max_length=ANSWER_MAX_CHARS
    )
    skipped: bool
    assessment: TurnAssessment | None = None

    @model_validator(mode="after")
    def _consistency(self) -> SnapshotTurn:
        if self.kind is TurnKind.main and self.main_index is None:
            raise ValueError("main_index обязателен для kind=main")
        if self.kind is TurnKind.follow_up and self.main_index is not None:
            raise ValueError("main_index недопустим для kind=follow_up")
        if self.skipped:
            if self.answer_text is not None or self.assessment is not None:
                raise ValueError("пропущенный turn не имеет ответа и оценки")
        elif self.answer_text is None:
            raise ValueError("не пропущенный turn обязан иметь подтверждённый ответ")
        if self.assessment is not None and self.assessment.turn_id != self.turn_id:
            raise ValueError("assessment.turn_id должен совпадать с turn_id снимка")
        return self


class GenerateReportInput(StrictModel):
    session_id: UUID
    role_id: RoleId
    level: Level
    partial: bool
    snapshot_turns: list[SnapshotTurn] = Field(min_length=1, max_length=8)


class Coverage(StrictModel):
    main_total: int = Field(ge=0, le=6)
    main_answered: int = Field(ge=0, le=6)
    main_skipped: int = Field(ge=0, le=6)
    follow_ups: int = Field(ge=0, le=2)

    @model_validator(mode="after")
    def _sum(self) -> Coverage:
        if self.main_answered + self.main_skipped != self.main_total:
            raise ValueError("main_answered + main_skipped должны равняться main_total")
        return self


class ReportFinding(StrictModel):
    """Наблюдение (сильная сторона либо ошибка/пробел). Для пропуска quote=null, skipped=true."""

    turn_id: UUID
    quote: str | None = Field(default=None, min_length=1)
    skipped: bool = False
    comment: str = Field(min_length=1)

    @model_validator(mode="after")
    def _quote_pairs(self) -> ReportFinding:
        if self.skipped and self.quote is not None:
            raise ValueError("у пропущенного turn quote обязан быть null")
        if not self.skipped and self.quote is None:
            raise ValueError("наблюдение по ответу обязано содержать цитату")
        return self


class ReportAction(StrictModel):
    priority: int = Field(ge=1, le=3)
    related_turn_id: UUID | None = None
    text: str = Field(min_length=1)


class ImprovedExample(StrictModel):
    turn_id: UUID
    text: str = Field(min_length=1)
    uses_placeholders: bool = False


class FeedbackReport(StrictModel):
    session_id: UUID
    partial: bool
    coverage: Coverage
    strengths: list[ReportFinding] = Field(default_factory=list, max_length=5)
    gaps: list[ReportFinding] = Field(default_factory=list, max_length=5)
    actions: list[ReportAction] = Field(min_length=1, max_length=3)
    improved_examples: list[ImprovedExample] = Field(default_factory=list, max_length=3)


# ---------------------------------------------------------------------------
# AI-контракты: голос (§3.5)
# ---------------------------------------------------------------------------


class GenerateSTTInput(StrictModel):
    storage_path: str = Field(min_length=1)
    input_format: Literal["ogg_opus", "pcm_s16le"]
    duration_seconds: float = Field(gt=0, le=120)
    language: Literal["ru"] = "ru"


class Transcription(StrictModel):
    text: str
    language: Literal["ru"] = "ru"
    duration_seconds: float | None = Field(default=None, gt=0)


class GenerateTTSInput(StrictModel):
    text: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    language: Literal["ru"] = "ru"
    output_format: Literal["ogg_opus"] = "ogg_opus"


class SynthesizedAudio(StrictModel):
    storage_key: str = Field(min_length=1)
    mime_type: Literal["audio/ogg", "audio/opus"]
    duration_seconds: float = Field(gt=0)


# ---------------------------------------------------------------------------
# Платежи (коммерческий этап, §3.8) — формат для проверки successful_payment
# ---------------------------------------------------------------------------


class SuccessfulPaymentPayload(StrictModel):
    """Формат Telegram successful_payment (XTR). Пример 75 XTR не устанавливает цену."""

    currency: Literal["XTR"]
    total_amount: int = Field(gt=0)
    invoice_payload: str = Field(min_length=1, max_length=64)
    telegram_payment_charge_id: str = Field(min_length=1)
    provider_payment_charge_id: str | None = None
