"""Генерация нормативных примеров contracts/examples + manifest.json.

Примеры соответствуют §3.1 (валидный start_session, невалидные уровень/формат,
пустой и слишком длинный ответ, score=5, неизвестное поле) и §3.8
(successful-payment 75 XTR — проверка формата, не цена продукта).

Запуск: python scripts/make_examples.py
Валидные примеры при генерации проверяются нормативными моделями.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contracts.models import (  # noqa: E402
    FeedbackReport,
    QuestionType,
    SnapshotTurn,
    StartSession,
    SuccessfulPaymentPayload,
    TurnAssessment,
)
from contracts.semantic_validation import validate_assessment, validate_report  # noqa: E402

EXAMPLES = ROOT / "contracts" / "examples"

SESSION_ID = "3f2b8c1e-9d4a-4e7b-8a1c-2d5e6f708001"
TURN_1 = "3f2b8c1e-9d4a-4e7b-8a1c-2d5e6f708002"
TURN_2 = "3f2b8c1e-9d4a-4e7b-8a1c-2d5e6f708003"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"

TECH_ANSWER = (
    "Список изменяемый и упорядоченный, кортеж неизменяемый. "
    "Поиск в словаре работает за O(1), потому что dict реализован как хеш-таблица. "
    "Множество использую, когда нужна уникальность элементов."
)
TECH_QUOTES = [
    "Поиск в словаре работает за O(1), потому что dict реализован как хеш-таблица",
    "кортеж неизменяемый",
]
HR_ANSWER = (
    "Я учусь на третьем курсе и пишу диплом по анализу данных. "
    "В учебном проекте собирал данные опросов в Python и считал статистику. "
    "На стажировке хочу получить опыт работы с реальными данными и ревью кода."
)
HR_QUOTES = ["В учебном проекте собирал данные опросов в Python и считал статистику"]

SNAPSHOT = [
    {
        "turn_id": TURN_1,
        "ordinal": 1,
        "kind": "main",
        "question_type": "technical",
        "main_index": 3,
        "topic": "collections",
        "question": "Чем dict и set отличаются от list по скорости поиска?",
        "answer_text": TECH_ANSWER,
        "skipped": False,
        "assessment": {
            "turn_id": TURN_1,
            "scores": {"relevance": 4, "structure": 3, "correctness": 3, "evidence": None},
            "missing_points": ["Не упомянуты коллизии хеширования"],
            "evidence_quotes": [TECH_QUOTES[0]],
            "explanation": "Ответ по существу, верные сложности, структура могла быть чётче.",
            "suggested_action": "next_topic",
            "next_question": None,
        },
    },
    {
        "turn_id": TURN_2,
        "ordinal": 2,
        "kind": "main",
        "question_type": "technical",
        "main_index": 4,
        "topic": "exceptions",
        "question": "Как работает try/except и зачем нужен finally?",
        "answer_text": None,
        "skipped": True,
        "assessment": None,
    },
]

REPORT_VALID = {
    "session_id": SESSION_ID,
    "partial": True,
    "coverage": {"main_total": 2, "main_answered": 1, "main_skipped": 1, "follow_ups": 0},
    "strengths": [
        {
            "turn_id": TURN_1,
            "quote": TECH_QUOTES[0],
            "skipped": False,
            "comment": "Точно объяснена причина быстрого поиска в словаре через хеш-таблицу.",
        }
    ],
    "gaps": [
        {
            "turn_id": TURN_2,
            "quote": None,
            "skipped": True,
            "comment": "Тема исключений пропущена — это базовый материал для junior-разработчика.",
        },
        {
            "turn_id": TURN_1,
            "quote": TECH_QUOTES[1],
            "skipped": False,
            "comment": "Ответ на вопрос о скорости поиска неполный: не упомянуты коллизии хеширования.",
        },
    ],
    "actions": [
        {
            "priority": 1,
            "related_turn_id": TURN_2,
            "text": "Разберите блоки try/except/else/finally и решите 3 задачи на обработку ошибок файлов.",
        },
        {
            "priority": 2,
            "related_turn_id": TURN_1,
            "text": "Повторите устройство хеш-таблицы: коллизии, амортизированную сложность, поведение при росте.",
        },
    ],
    "improved_examples": [
        {
            "turn_id": TURN_1,
            "text": (
                "dict и set реализованы на хеш-таблицах, поэтому поиск в них в среднем O(1), "
                "а в list — O(n). При коллизиях сложность ухудшается, но амортизированно "
                "остаётся константной. Set выберу для проверки уникальности, dict — для пар ключ-значение."
            ),
            "uses_placeholders": False,
        }
    ],
}


def _write(name: str, payload: object) -> None:
    (EXAMPLES / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    EXAMPLES.mkdir(parents=True, exist_ok=True)

    # --- §3.1: валидный и невалидные start_session, неизвестное поле ---
    valid_start = {"role_id": "python", "level": "junior", "format": "voice"}
    StartSession.model_validate(valid_start)  # самопроверка
    _write("start-session-valid.json", valid_start)
    _write("start-session-invalid-enum.json", {"role_id": "python", "level": "senior", "format": "video"})
    _write(
        "start-session-unknown-field.json",
        {"role_id": "python", "level": "junior", "format": "text", "actor_user_id": 42},
    )

    # --- §3.1/AT09: граничные длины ответа 0 / 4000 / 4001 ---
    _write("submit-answer-empty.json", {"session_id": SESSION_ID, "turn_id": TURN_1, "text": ""})
    _write(
        "submit-answer-max-valid.json",
        {"session_id": SESSION_ID, "turn_id": TURN_1, "text": "а" * 4000},
    )
    _write(
        "submit-answer-too-long.json",
        {"session_id": SESSION_ID, "turn_id": TURN_1, "text": "а" * 4001},
    )

    # --- §3.2: единый ErrorResponse (точный пример из ТЗ) ---
    _write(
        "error-response.json",
        {
            "ok": False,
            "error": {
                "code": "ANSWER_TOO_LONG",
                "message": "Ответ должен содержать не более 4000 символов.",
                "retryable": False,
                "field": "text",
                "retry_after_seconds": None,
            },
            "request_id": REQUEST_ID,
        },
    )

    # --- TurnAssessment: валидные technical/hr, невалидные score и цитата ---
    tech_valid = {
        "turn_id": TURN_1,
        "scores": {"relevance": 4, "structure": 3, "correctness": 3, "evidence": None},
        "missing_points": ["Не упомянуты коллизии хеширования"],
        "evidence_quotes": TECH_QUOTES,
        "explanation": "Ответ по существу вопроса; верная сложность операций; структура могла быть чётче.",
        "suggested_action": "next_topic",
        "next_question": "Почему при большом числе коллизий поиск в словаре деградирует и как Python это ограничивает?",
    }
    model = TurnAssessment.model_validate(tech_valid)
    issues = validate_assessment(model, question_type=QuestionType.technical, answer_text=TECH_ANSWER)
    assert not issues, issues
    _write("turn-assessment-technical-valid.json", tech_valid)

    hr_valid = {
        "turn_id": TURN_2,
        "scores": {"relevance": 3, "structure": 3, "correctness": None, "evidence": 3},
        "missing_points": ["Не названы конкретные инструменты проекта"],
        "evidence_quotes": HR_QUOTES,
        "explanation": "Есть конкретные факты об учёбе и проекте; мотивация понятна; детали инструментов стоит раскрыть.",
        "suggested_action": "next_topic",
        "next_question": None,
    }
    model = TurnAssessment.model_validate(hr_valid)
    issues = validate_assessment(model, question_type=QuestionType.hr, answer_text=HR_ANSWER)
    assert not issues, issues
    _write("turn-assessment-hr-valid.json", hr_valid)

    _write(
        "turn-assessment-invalid-score.json",
        {**tech_valid, "scores": {"relevance": 4, "structure": 3, "correctness": 5, "evidence": None}},
    )
    _write(
        "turn-assessment-invalid-quote.json",
        {**tech_valid, "evidence_quotes": ["Словарь реализован как красно-чёрное дерево"]},
    )
    _write(
        "turn-assessment-both-topic-scores.json",
        {**tech_valid, "scores": {"relevance": 4, "structure": 3, "correctness": 3, "evidence": 2}},
    )

    # --- FeedbackReport: валидный и два невалидных ---
    report = FeedbackReport.model_validate(REPORT_VALID)
    issues = validate_report(
        report,
        snapshot=[SnapshotTurn.model_validate(t) for t in SNAPSHOT],
        expected_session_id=report.session_id,
        expected_partial=True,
    )
    assert not issues, issues
    _write("feedback-report-valid.json", REPORT_VALID)

    bad_strengths = json.loads(json.dumps(REPORT_VALID))
    bad_strengths["strengths"].append(
        {"turn_id": TURN_2, "quote": None, "skipped": True, "comment": "Пропуск темы засчитан как сила."}
    )
    _write("feedback-report-skipped-in-strengths.json", bad_strengths)

    bad_priorities = json.loads(json.dumps(REPORT_VALID))
    bad_priorities["actions"][1]["priority"] = 3
    _write("feedback-report-priorities-bad.json", bad_priorities)

    # --- §3.8: successful_payment 75 XTR — проверка формата, не цена ---
    payment = {
        "currency": "XTR",
        "total_amount": 75,
        "invoice_payload": "ord_01J8ZEXAMPLEPAYLOAD",
        "telegram_payment_charge_id": "tg_charge_01J8ZEXAMPLE",
        "provider_payment_charge_id": None,
    }
    SuccessfulPaymentPayload.model_validate(payment)
    _write("successful-payment.json", payment)

    # --- manifest.json ---
    manifest = {
        "version": "2.0",
        "note": "Проверяемые примеры контрактов: валидные и невалидные тела. stage: structural — нормативная модель Pydantic; domain — доменная валидация (app/domain); semantic — contracts/semantic_validation.py. Пример 75 XTR служит проверке формата и не устанавливает цену продукта.",
        "examples": [
            {
                "file": "start-session-valid.json",
                "description": "Валидный start_session (§3.1): python/junior/voice",
                "model": "StartSession",
                "stage": "structural",
                "valid": True,
            },
            {
                "file": "start-session-invalid-enum.json",
                "description": "Неподдерживаемые уровень и формат (§3.1)",
                "model": "StartSession",
                "stage": "structural",
                "valid": False,
                "expected_error_codes": ["UNSUPPORTED_LEVEL", "UNSUPPORTED_FORMAT"],
                "internal_error_code": "INVALID_INPUT",
            },
            {
                "file": "start-session-unknown-field.json",
                "description": "Дополнительный actor_user_id — неизвестное поле (§3.1)",
                "model": "StartSession",
                "stage": "structural",
                "valid": False,
                "expected_error_codes": ["INVALID_INPUT"],
                "expected_field": "actor_user_id",
            },
            {
                "file": "submit-answer-empty.json",
                "description": 'text="" — EMPTY_ANSWER (§3.1)',
                "model": "SubmitAnswer",
                "stage": "domain",
                "valid": False,
                "expected_error_codes": ["EMPTY_ANSWER"],
            },
            {
                "file": "submit-answer-max-valid.json",
                "description": "Граничные 4000 кодовых точек — принимается (AT09)",
                "model": "SubmitAnswer",
                "stage": "domain",
                "valid": True,
            },
            {
                "file": "submit-answer-too-long.json",
                "description": "Строка из 4001 символа — ANSWER_TOO_LONG, не обрезать (§3.1)",
                "model": "SubmitAnswer",
                "stage": "domain",
                "valid": False,
                "expected_error_codes": ["ANSWER_TOO_LONG"],
            },
            {
                "file": "error-response.json",
                "description": "Единый ErrorResponse (§3.2), точный пример из ТЗ",
                "model": "ErrorResponse",
                "stage": "structural",
                "valid": True,
            },
            {
                "file": "turn-assessment-technical-valid.json",
                "description": "TurnAssessment 2.0 technical: correctness обязателен, evidence=null",
                "model": "TurnAssessment",
                "stage": "semantic",
                "valid": True,
                "semantic_context": {"question_type": "technical", "answer_text": TECH_ANSWER},
            },
            {
                "file": "turn-assessment-hr-valid.json",
                "description": "TurnAssessment 2.0 hr: evidence обязателен, correctness=null",
                "model": "TurnAssessment",
                "stage": "semantic",
                "valid": True,
                "semantic_context": {"question_type": "hr", "answer_text": HR_ANSWER},
            },
            {
                "file": "turn-assessment-invalid-score.json",
                "description": "score=5 — нарушение диапазона 0..4 (§3.1)",
                "model": "TurnAssessment",
                "stage": "structural",
                "valid": False,
                "expected_error_codes": ["INVALID_INPUT"],
                "expected_field": "scores.correctness",
            },
            {
                "file": "turn-assessment-invalid-quote.json",
                "description": "Цитата отсутствует в фактическом ответе — QUOTE_NOT_FOUND",
                "model": "TurnAssessment",
                "stage": "semantic",
                "valid": False,
                "expected_error_codes": ["QUOTE_NOT_FOUND"],
                "semantic_context": {"question_type": "technical", "answer_text": TECH_ANSWER},
            },
            {
                "file": "turn-assessment-both-topic-scores.json",
                "description": "correctness и evidence одновременно — структурная ошибка",
                "model": "TurnAssessment",
                "stage": "structural",
                "valid": False,
                "expected_error_codes": ["INVALID_INPUT"],
            },
            {
                "file": "feedback-report-valid.json",
                "description": "FeedbackReport 2.0: цитаты проверяемы, приоритеты 1..n, действие связано с пробелом",
                "model": "FeedbackReport",
                "stage": "semantic",
                "valid": True,
                "semantic_context": {"snapshot": SNAPSHOT, "expected_partial": True},
            },
            {
                "file": "feedback-report-skipped-in-strengths.json",
                "description": "Пропуск в strengths запрещён (§3.4)",
                "model": "FeedbackReport",
                "stage": "semantic",
                "valid": False,
                "expected_error_codes": ["SKIPPED_IN_STRENGTHS"],
                "semantic_context": {"snapshot": SNAPSHOT, "expected_partial": True},
            },
            {
                "file": "feedback-report-priorities-bad.json",
                "description": "Приоритеты не последовательны от 1 (§3.4)",
                "model": "FeedbackReport",
                "stage": "semantic",
                "valid": False,
                "expected_error_codes": ["PRIORITIES_NOT_SEQUENTIAL"],
                "semantic_context": {"snapshot": SNAPSHOT, "expected_partial": True},
            },
            {
                "file": "successful-payment.json",
                "description": "Формат successful_payment XTR (§3.8); 75 XTR — пример формата, не цена",
                "model": "SuccessfulPaymentPayload",
                "stage": "structural",
                "valid": True,
            },
        ],
    }
    (EXAMPLES / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"OK: {len(manifest['examples'])} примеров -> {EXAMPLES}")


if __name__ == "__main__":
    main()
