"""Семантическая валидация AI-результатов сверх структурной схемы (§3.3–3.4).

Проверяет цитаты и ссылки на фактические данные снимка/ответа:
- цитата обязана буквально присутствовать в ответе после Unicode NFC и
  унификации пробельных последовательностей;
- все turn_id обязаны существовать в снимке;
- пропуски запрещены в strengths;
- приоритеты действий последовательны от 1 без повторов;
- при наличии gaps хотя бы одно действие связано с пробелом.

Также содержит детерминированные серверные правила §3.3 (допустимое действие
и шаг сложности): решение принимает сервер, мнение модели принимается только
если совпадает с допустимым действием.

Модуль не зависит от app/*: чистые функции над нормативными моделями.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from uuid import UUID

from contracts.models import (
    ANSWER_MAX_CHARS,
    QUESTION_MAX_CHARS,
    FeedbackReport,
    QuestionType,
    SnapshotTurn,
    SuggestedAction,
    TurnAssessment,
    TurnKind,
    TurnScores,
)

SEMANTIC_VALIDATION_VERSION = "2.0"


# ---------------------------------------------------------------------------
# Нормализация цитат
# ---------------------------------------------------------------------------


def normalize_for_quote(text: str) -> str:
    """NFC + сведение пробельных последовательностей к одному пробелу + strip."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def quote_matches(quote: str, answer_text: str) -> bool:
    """Цитата буквально присутствует в ответе после нормализации (§3.3)."""
    return normalize_for_quote(quote) in normalize_for_quote(answer_text)


# ---------------------------------------------------------------------------
# Проблемы (issues): детерминированный результат валидации
# ---------------------------------------------------------------------------


class SemanticIssueCode:
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    SCORE_FIELD_MISMATCH = "SCORE_FIELD_MISMATCH"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    NEXT_QUESTION_REQUIRED = "NEXT_QUESTION_REQUIRED"
    NEXT_QUESTION_INVALID = "NEXT_QUESTION_INVALID"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    PARTIAL_MISMATCH = "PARTIAL_MISMATCH"
    TURN_NOT_IN_SNAPSHOT = "TURN_NOT_IN_SNAPSHOT"
    COVERAGE_MISMATCH = "COVERAGE_MISMATCH"
    SKIPPED_IN_STRENGTHS = "SKIPPED_IN_STRENGTHS"
    QUOTE_REQUIRED = "QUOTE_REQUIRED"
    QUOTE_ON_SKIPPED = "QUOTE_ON_SKIPPED"
    PRIORITIES_NOT_SEQUENTIAL = "PRIORITIES_NOT_SEQUENTIAL"
    ACTION_NOT_LINKED_TO_TURN = "ACTION_NOT_LINKED_TO_TURN"
    ACTION_NOT_LINKED_TO_GAP = "ACTION_NOT_LINKED_TO_GAP"


@dataclass(frozen=True)
class SemanticIssue:
    code: str
    message: str
    path: str | None = None


# ---------------------------------------------------------------------------
# Серверные правила §3.3: действие и сложность
# ---------------------------------------------------------------------------

DONT_KNOW_PATTERNS = (
    "не знаю",
    "незнаю",
    "не знаю.",
    "не в курсе",
    "затрудняюсь ответить",
    "нет ответа",
    "не могу ответить",
    "не могу сказать",
    "pass",
    "не разбирался",
    "не сталкивался",
    "не сталкивалась",
)


def is_dont_know(answer_text: str) -> bool:
    """Нормализованное «не знаю»: короткий прямой отказ отвечать по существу."""
    normalized = normalize_for_quote(answer_text).lower().strip(" .,!…-—?")
    return normalized in DONT_KNOW_PATTERNS


def topic_score(scores: TurnScores, question_type: QuestionType) -> int | None:
    """Тематическая оценка: correctness для technical, evidence для HR."""
    return scores.correctness if question_type is QuestionType.technical else scores.evidence


def compute_allowed_action(
    *,
    question_type: QuestionType,
    scores: TurnScores,
    missing_points: list[str],
    follow_ups_remaining: int,
    parent_has_follow_up: bool,
    answer_text: str,
) -> SuggestedAction:
    """Фиксированное правило сервера (§3.3).

    1. relevance=0 либо нормализованное «не знаю» -> next_topic (без уточнения).
    2. Тематическая оценка <= 2, relevance >= 1, missing_points непуст ->
       уточнение разрешено при оставшемся лимите и отсутствии уточнения
       у данного основного вопроса.
    3. Иначе -> next_topic.
    """
    if scores.relevance == 0 or is_dont_know(answer_text):
        return SuggestedAction.next_topic
    tscore = topic_score(scores, question_type)
    if (
        tscore is not None
        and tscore <= 2
        and scores.relevance >= 1
        and missing_points
        and follow_ups_remaining > 0
        and not parent_has_follow_up
    ):
        return SuggestedAction.follow_up
    return SuggestedAction.next_topic


def compute_next_difficulty(
    *,
    current: int,
    difficulty_min: int,
    difficulty_max: int,
    question_type: QuestionType,
    is_main_answer: bool,
    scores: TurnScores,
) -> int:
    """Шаг сложности следующего технического вопроса (§3.3).

    Применяется после основного ответа на технический вопрос:
    +1 при тематической оценке >= 3 и relevance >= 3; -1 при тематической <= 1;
    иначе сохранить. HR техническую сложность не меняет; ответ на уточнение
    сложность не меняет. Границы уровня соблюдаются.
    """
    if question_type is not QuestionType.technical or not is_main_answer:
        return current
    tscore = topic_score(scores, question_type)
    if tscore is None:
        return current
    delta = 0
    if tscore >= 3 and scores.relevance >= 3:
        delta = 1
    elif tscore <= 1:
        delta = -1
    return max(difficulty_min, min(difficulty_max, current + delta))


# ---------------------------------------------------------------------------
# Валидация TurnAssessment (§3.3)
# ---------------------------------------------------------------------------


def validate_question_text(question: str) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    if not question.strip():
        issues.append(
            SemanticIssue(SemanticIssueCode.NEXT_QUESTION_INVALID, "вопрос пуст", "next_question")
        )
        return issues
    if len(question) > QUESTION_MAX_CHARS:
        issues.append(
            SemanticIssue(
                SemanticIssueCode.NEXT_QUESTION_INVALID,
                f"вопрос длиннее {QUESTION_MAX_CHARS} символов",
                "next_question",
            )
        )
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n" for ch in question):
        issues.append(
            SemanticIssue(
                SemanticIssueCode.NEXT_QUESTION_INVALID,
                "вопрос содержит управляющие символы",
                "next_question",
            )
        )
    return issues


def validate_assessment(
    assessment: TurnAssessment,
    *,
    question_type: QuestionType,
    answer_text: str,
    allowed_action: SuggestedAction | None = None,
) -> list[SemanticIssue]:
    """Семантические проверки оценки ответа.

    question_type-зависимые пары оценок, дословность цитат, допустимость
    действия. Тематическая принадлежность следующего вопроса проверяется
    доменом (часть 3) совместно с каталогом.
    """
    issues: list[SemanticIssue] = []

    if question_type is QuestionType.technical:
        if assessment.scores.correctness is None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.SCORE_FIELD_MISMATCH,
                    "для технического вопроса correctness обязателен",
                    "scores.correctness",
                )
            )
        if assessment.scores.evidence is not None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.SCORE_FIELD_MISMATCH,
                    "для технического вопроса evidence должен быть null",
                    "scores.evidence",
                )
            )
    else:
        if assessment.scores.evidence is None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.SCORE_FIELD_MISMATCH,
                    "для HR-вопроса evidence обязателен",
                    "scores.evidence",
                )
            )
        if assessment.scores.correctness is not None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.SCORE_FIELD_MISMATCH,
                    "для HR-вопроса correctness должен быть null",
                    "scores.correctness",
                )
            )

    if len(answer_text) > ANSWER_MAX_CHARS:
        # Защита вызывающего кода: ответ обязан быть нормализован до оценки.
        issues.append(
            SemanticIssue(
                SemanticIssueCode.QUOTE_NOT_FOUND,
                "ответ превышает предел длины — проверка цитат недостоверна",
                "answer_text",
            )
        )

    for i, quote in enumerate(assessment.evidence_quotes):
        if not quote_matches(quote, answer_text):
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.QUOTE_NOT_FOUND,
                    "цитата не найдена в фактическом ответе",
                    f"evidence_quotes[{i}]",
                )
            )

    if allowed_action is not None and assessment.suggested_action is not allowed_action:
        issues.append(
            SemanticIssue(
                SemanticIssueCode.ACTION_NOT_ALLOWED,
                f"действие модели {assessment.suggested_action.value} не совпадает "
                f"с допустимым {allowed_action.value}",
                "suggested_action",
            )
        )

    if assessment.suggested_action is SuggestedAction.follow_up:
        if assessment.next_question is None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.NEXT_QUESTION_REQUIRED,
                    "для follow_up обязателен next_question",
                    "next_question",
                )
            )
        else:
            issues.extend(validate_question_text(assessment.next_question))
    elif assessment.next_question is not None:
        issues.extend(validate_question_text(assessment.next_question))

    return issues


# ---------------------------------------------------------------------------
# Валидация FeedbackReport (§3.4)
# ---------------------------------------------------------------------------


def validate_report(
    report: FeedbackReport,
    *,
    snapshot: list[SnapshotTurn],
    expected_session_id: UUID,
    expected_partial: bool,
) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    turns_by_id: dict[UUID, SnapshotTurn] = {t.turn_id: t for t in snapshot}

    if report.session_id != expected_session_id:
        issues.append(
            SemanticIssue(SemanticIssueCode.SESSION_MISMATCH, "session_id не совпадает со снимком")
        )
    if report.partial != expected_partial:
        issues.append(
            SemanticIssue(SemanticIssueCode.PARTIAL_MISMATCH, "partial не совпадает со снимком")
        )

    # coverage обязан точно соответствовать снимку
    main_turns = [t for t in snapshot if t.kind is TurnKind.main]
    expected_coverage = {
        "main_total": len(main_turns),
        "main_answered": sum(1 for t in main_turns if not t.skipped),
        "main_skipped": sum(1 for t in main_turns if t.skipped),
        "follow_ups": sum(1 for t in snapshot if t.kind is TurnKind.follow_up),
    }
    actual_coverage = {
        "main_total": report.coverage.main_total,
        "main_answered": report.coverage.main_answered,
        "main_skipped": report.coverage.main_skipped,
        "follow_ups": report.coverage.follow_ups,
    }
    if actual_coverage != expected_coverage:
        issues.append(
            SemanticIssue(
                SemanticIssueCode.COVERAGE_MISMATCH,
                f"coverage {actual_coverage} не соответствует снимку {expected_coverage}",
                "coverage",
            )
        )

    def _check_turn_ref(turn_id: UUID, path: str) -> SnapshotTurn | None:
        turn = turns_by_id.get(turn_id)
        if turn is None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.TURN_NOT_IN_SNAPSHOT,
                    f"turn_id {turn_id} отсутствует в снимке",
                    path,
                )
            )
        return turn

    def _check_findings(findings: list, section: str, allow_skipped: bool) -> None:
        for i, finding in enumerate(findings):
            path = f"{section}[{i}]"
            turn = _check_turn_ref(finding.turn_id, f"{path}.turn_id")
            if turn is None:
                continue
            if finding.skipped:
                if not allow_skipped:
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.SKIPPED_IN_STRENGTHS,
                            "пропущенный вопрос нельзя указывать в сильных сторонах",
                            path,
                        )
                    )
                if not turn.skipped:
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.QUOTE_ON_SKIPPED,
                            "finding.skipped=true противоречит принятому ответу в снимке",
                            f"{path}.skipped",
                        )
                    )
                if finding.quote is not None:
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.QUOTE_ON_SKIPPED,
                            "у пропущенного turn quote обязан быть null",
                            f"{path}.quote",
                        )
                    )
            else:
                if turn.skipped:
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.QUOTE_ON_SKIPPED,
                            "finding без skipped=true ссылается на пропущенный turn",
                            f"{path}.skipped",
                        )
                    )
                elif finding.quote is None:
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.QUOTE_REQUIRED,
                            "наблюдение по ответу обязано содержать цитату",
                            f"{path}.quote",
                        )
                    )
                elif turn.answer_text is None or not quote_matches(finding.quote, turn.answer_text):
                    issues.append(
                        SemanticIssue(
                            SemanticIssueCode.QUOTE_NOT_FOUND,
                            "цитата не найдена в фактическом ответе turn",
                            f"{path}.quote",
                        )
                    )

    _check_findings(report.strengths, "strengths", allow_skipped=False)
    _check_findings(report.gaps, "gaps", allow_skipped=True)

    # Приоритеты последовательны от 1 без повторов
    priorities = [a.priority for a in report.actions]
    if sorted(priorities) != list(range(1, len(priorities) + 1)):
        issues.append(
            SemanticIssue(
                SemanticIssueCode.PRIORITIES_NOT_SEQUENTIAL,
                f"приоритеты {priorities} не образуют последовательность 1..{len(priorities)}",
                "actions",
            )
        )

    # Каждое действие связано с разобранным вопросом; при наличии gaps хотя бы
    # одно действие связано с пробелом.
    gap_turn_ids = {f.turn_id for f in report.gaps}
    linked_any = False
    linked_to_gap = False
    for i, action in enumerate(report.actions):
        if action.related_turn_id is None:
            issues.append(
                SemanticIssue(
                    SemanticIssueCode.ACTION_NOT_LINKED_TO_TURN,
                    "действие не связано с разобранным вопросом",
                    f"actions[{i}].related_turn_id",
                )
            )
            continue
        if _check_turn_ref(action.related_turn_id, f"actions[{i}].related_turn_id") is not None:
            linked_any = True
            if action.related_turn_id in gap_turn_ids:
                linked_to_gap = True
    if gap_turn_ids and not linked_to_gap:
        issues.append(
            SemanticIssue(
                SemanticIssueCode.ACTION_NOT_LINKED_TO_GAP,
                "при наличии gaps хотя бы одно действие обязано ссылаться на пробел",
                "actions",
            )
        )
    if not linked_any:
        issues.append(
            SemanticIssue(
                SemanticIssueCode.ACTION_NOT_LINKED_TO_TURN,
                "ни одно действие не связано с разобранными вопросами",
                "actions",
            )
        )

    for i, example in enumerate(report.improved_examples):
        _check_turn_ref(example.turn_id, f"improved_examples[{i}].turn_id")

    return issues
