"""Семантическая валидация и фиксированные серверные правила (§3.3–3.4)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.models import (
    Coverage,
    FeedbackReport,
    ImprovedExample,
    QuestionType,
    ReportAction,
    ReportFinding,
    SnapshotTurn,
    SuggestedAction,
    TurnAssessment,
    TurnKind,
    TurnScores,
)
from contracts.semantic_validation import (
    SemanticIssueCode,
    compute_allowed_action,
    compute_next_difficulty,
    is_dont_know,
    normalize_for_quote,
    quote_matches,
    validate_assessment,
    validate_report,
)

SESSION = uuid4()
T1 = uuid4()
T2 = uuid4()

TECH_ANSWER = (
    "Список изменяемый, кортеж неизменяемый.\n"
    "  Поиск в словаре — O(1), потому   что это хеш-таблица."
)


def make_assessment(**overrides) -> TurnAssessment:
    base = {
        "turn_id": T1,
        "scores": {"relevance": 3, "structure": 3, "correctness": 2, "evidence": None},
        "missing_points": ["Не упомянуты коллизии"],
        "evidence_quotes": ["Поиск в словаре — O(1)"],
        "explanation": "Базовое понимание есть, детали пропущены.",
        "suggested_action": "next_topic",
        "next_question": None,
    }
    base.update(overrides)
    return TurnAssessment.model_validate(base)


# ---------------------------------------------------------------------------
# Нормализация цитат (§3.3: NFC + унификация пробельных последовательностей)
# ---------------------------------------------------------------------------


def test_normalize_nfc_and_whitespace() -> None:
    decomposed = "е\u0308  жо\tдикое"  # decomposed umlaut + табы
    composed = "ё  жо   дикое"
    assert normalize_for_quote(decomposed) == normalize_for_quote(composed)
    assert normalize_for_quote("  a \n\n b  ") == "a b"


def test_quote_matches_with_whitespace_variants() -> None:
    assert quote_matches("Поиск в словаре — O(1)", TECH_ANSWER.replace("   ", " "))
    # цитата с другими пробельными последовательностями всё равно находится
    assert quote_matches("Поиск в  словаре —\nO(1)", TECH_ANSWER)
    assert quote_matches("кортеж неизменяемый", TECH_ANSWER)
    assert not quote_matches("красно-чёрное дерево", TECH_ANSWER)


def test_quote_matches_multiline_answer_keeps_internal_indent() -> None:
    # внутренние отступы сохраняются, но сравнение цитат сводит пробелы
    assert quote_matches("Поиск в словаре — O(1), потому что это хеш-таблица", TECH_ANSWER)


# ---------------------------------------------------------------------------
# «Не знаю» (§3.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["не знаю", "Не знаю.", "  не   знаю ", "не знаю!", "затрудняюсь ответить", "НЕ ЗНАЮ"],
)
def test_is_dont_know_positive(text: str) -> None:
    assert is_dont_know(text)


@pytest.mark.parametrize(
    "text", ["я не знаю точно, но предполагаю, что dict", "не уверен", "знаю частично"]
)
def test_is_dont_know_negative(text: str) -> None:
    assert not is_dont_know(text)


# ---------------------------------------------------------------------------
# Фиксированное правило действия (§3.3)
# ---------------------------------------------------------------------------


def scores(relevance: int, structure: int, correctness=None, evidence=None) -> TurnScores:
    return TurnScores(
        relevance=relevance, structure=structure, correctness=correctness, evidence=evidence
    )


def action(**kw) -> SuggestedAction:
    base = {
        "question_type": QuestionType.technical,
        "scores": scores(3, 3, correctness=2),
        "missing_points": ["пробел"],
        "follow_ups_remaining": 1,
        "parent_has_follow_up": False,
        "answer_text": "частичный ответ",
    }
    base.update(kw)
    return compute_allowed_action(**base)


def test_action_follow_up_when_weak_answer_and_limit_available() -> None:
    assert action() is SuggestedAction.follow_up


def test_action_next_when_relevance_zero() -> None:
    assert action(scores=scores(0, 1, correctness=1)) is SuggestedAction.next_topic


def test_action_next_when_dont_know() -> None:
    assert action(answer_text="не знаю") is SuggestedAction.next_topic


def test_action_next_when_limit_exhausted() -> None:
    assert action(follow_ups_remaining=0) is SuggestedAction.next_topic


def test_action_next_when_parent_already_has_follow_up() -> None:
    assert action(parent_has_follow_up=True) is SuggestedAction.next_topic


def test_action_next_when_no_missing_points() -> None:
    assert action(missing_points=[]) is SuggestedAction.next_topic


def test_action_next_when_strong_answer() -> None:
    assert action(scores=scores(4, 4, correctness=3)) is SuggestedAction.next_topic


def test_action_hr_uses_evidence() -> None:
    assert (
        action(
            question_type=QuestionType.hr,
            scores=scores(2, 2, evidence=1),
        )
        is SuggestedAction.follow_up
    )


def test_model_suggestion_accepted_only_if_matches() -> None:
    """suggested_action модели принимается только если совпадает с допустимым."""
    assessment = make_assessment(suggested_action="follow_up", next_question="Уточните коллизии?")
    allowed = compute_allowed_action(
        question_type=QuestionType.technical,
        scores=assessment.scores,
        missing_points=assessment.missing_points,
        follow_ups_remaining=1,
        parent_has_follow_up=False,
        answer_text=TECH_ANSWER,
    )
    issues = validate_assessment(
        assessment, question_type=QuestionType.technical, answer_text=TECH_ANSWER,
        allowed_action=allowed,
    )
    assert not issues
    # серверное правило имеет приоритет: другое действие -> ACTION_NOT_ALLOWED
    issues = validate_assessment(
        assessment,
        question_type=QuestionType.technical,
        answer_text=TECH_ANSWER,
        allowed_action=SuggestedAction.next_topic,
    )
    assert any(i.code == SemanticIssueCode.ACTION_NOT_ALLOWED for i in issues)


# ---------------------------------------------------------------------------
# Шаг сложности (§3.3)
# ---------------------------------------------------------------------------


def test_difficulty_up() -> None:
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.technical,
            is_main_answer=True,
            scores=scores(3, 3, correctness=3),
        )
        == 3
    )


def test_difficulty_up_requires_relevance() -> None:
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.technical,
            is_main_answer=True,
            scores=scores(2, 3, correctness=4),
        )
        == 2
    )


def test_difficulty_down() -> None:
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.technical,
            is_main_answer=True,
            scores=scores(1, 1, correctness=1),
        )
        == 1
    )


def test_difficulty_clamped_to_level_bounds() -> None:
    # intern ограничен 1–2
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=2,
            question_type=QuestionType.technical,
            is_main_answer=True,
            scores=scores(4, 4, correctness=4),
        )
        == 2
    )
    # junior не опускается ниже 1
    assert (
        compute_next_difficulty(
            current=1,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.technical,
            is_main_answer=True,
            scores=scores(1, 1, correctness=0),
        )
        == 1
    )


def test_difficulty_kept_for_hr_and_follow_up() -> None:
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.hr,
            is_main_answer=True,
            scores=scores(4, 4, evidence=4),
        )
        == 2
    )
    assert (
        compute_next_difficulty(
            current=2,
            difficulty_min=1,
            difficulty_max=3,
            question_type=QuestionType.technical,
            is_main_answer=False,
            scores=scores(4, 4, correctness=4),
        )
        == 2
    )


# ---------------------------------------------------------------------------
# validate_assessment: пары оценок и цитаты (§3.3)
# ---------------------------------------------------------------------------


def test_technical_requires_correctness() -> None:
    assessment = make_assessment(
        scores={"relevance": 3, "structure": 3, "correctness": None, "evidence": None}
    )
    issues = validate_assessment(
        assessment, question_type=QuestionType.technical, answer_text=TECH_ANSWER
    )
    assert any(i.code == SemanticIssueCode.SCORE_FIELD_MISMATCH for i in issues)


def test_hr_requires_evidence_and_forbids_correctness() -> None:
    assessment = make_assessment(
        scores={"relevance": 3, "structure": 3, "correctness": 3, "evidence": None}
    )
    issues = validate_assessment(
        assessment, question_type=QuestionType.hr, answer_text=TECH_ANSWER
    )
    codes = {i.code for i in issues}
    assert SemanticIssueCode.SCORE_FIELD_MISMATCH in codes


def test_quote_must_be_literal_in_answer() -> None:
    assessment = make_assessment(evidence_quotes=["хеш-таблица реализована на BST"])
    issues = validate_assessment(
        assessment, question_type=QuestionType.technical, answer_text=TECH_ANSWER
    )
    assert any(
        i.code == SemanticIssueCode.QUOTE_NOT_FOUND and i.path == "evidence_quotes[0]"
        for i in issues
    )


def test_follow_up_requires_next_question() -> None:
    with pytest.raises(ValidationError):
        # структурная модель запрещает: follow_up без next_question
        make_assessment(suggested_action="follow_up", next_question=None)


def test_next_question_length_limit() -> None:
    with pytest.raises(ValidationError):
        make_assessment(suggested_action="follow_up", next_question="в" * 701)


# ---------------------------------------------------------------------------
# validate_report (§3.4)
# ---------------------------------------------------------------------------


def make_snapshot() -> list[SnapshotTurn]:
    return [
        SnapshotTurn.model_validate(
            {
                "turn_id": T1,
                "ordinal": 1,
                "kind": TurnKind.main,
                "question_type": QuestionType.technical,
                "main_index": 3,
                "topic": "collections",
                "question": "Чем dict отличается от list?",
                "answer_text": TECH_ANSWER,
                "skipped": False,
                "assessment": None,
            }
        ),
        SnapshotTurn.model_validate(
            {
                "turn_id": T2,
                "ordinal": 2,
                "kind": TurnKind.main,
                "question_type": QuestionType.technical,
                "main_index": 4,
                "topic": "exceptions",
                "question": "Как работает try/except?",
                "answer_text": None,
                "skipped": True,
                "assessment": None,
            }
        ),
    ]


def make_report(**overrides) -> FeedbackReport:
    base = {
        "session_id": SESSION,
        "partial": True,
        "coverage": Coverage(main_total=2, main_answered=1, main_skipped=1, follow_ups=0),
        "strengths": [
            ReportFinding(
                turn_id=T1,
                quote="Поиск в словаре — O(1)",
                skipped=False,
                comment="Верно названа сложность поиска.",
            )
        ],
        "gaps": [
            ReportFinding(turn_id=T2, quote=None, skipped=True, comment="Тема пропущена.")
        ],
        "actions": [
            ReportAction(priority=1, related_turn_id=T2, text="Разберите try/except."),
            ReportAction(priority=2, related_turn_id=T1, text="Повторите хеш-таблицы."),
        ],
        "improved_examples": [
            ImprovedExample(turn_id=T1, text="Улучшенный ответ", uses_placeholders=False)
        ],
    }
    base.update(overrides)
    return FeedbackReport.model_validate(base)


def _validate(report: FeedbackReport):
    return validate_report(
        report, snapshot=make_snapshot(), expected_session_id=SESSION, expected_partial=True
    )


def test_report_valid() -> None:
    assert _validate(make_report()) == []


def test_report_session_mismatch() -> None:
    issues = validate_report(
        make_report(),
        snapshot=make_snapshot(),
        expected_session_id=uuid4(),
        expected_partial=True,
    )
    assert any(i.code == SemanticIssueCode.SESSION_MISMATCH for i in issues)


def test_report_partial_mismatch() -> None:
    issues = validate_report(
        make_report(), snapshot=make_snapshot(), expected_session_id=SESSION, expected_partial=False
    )
    assert any(i.code == SemanticIssueCode.PARTIAL_MISMATCH for i in issues)


def test_report_coverage_mismatch() -> None:
    report = make_report(
        coverage=Coverage(main_total=2, main_answered=2, main_skipped=0, follow_ups=0)
    )
    assert any(i.code == SemanticIssueCode.COVERAGE_MISMATCH for i in _validate(report))


def test_report_turn_not_in_snapshot() -> None:
    report = make_report(
        improved_examples=[ImprovedExample(turn_id=uuid4(), text="x", uses_placeholders=False)]
    )
    assert any(i.code == SemanticIssueCode.TURN_NOT_IN_SNAPSHOT for i in _validate(report))


def test_report_skipped_in_strengths_forbidden() -> None:
    report = make_report(
        strengths=[ReportFinding(turn_id=T2, quote=None, skipped=True, comment="Пропуск")]
    )
    assert any(i.code == SemanticIssueCode.SKIPPED_IN_STRENGTHS for i in _validate(report))


def test_report_quote_must_match_answer() -> None:
    report = make_report(
        strengths=[
            ReportFinding(
                turn_id=T1, quote="выдуманная цитата", skipped=False, comment="Комментарий"
            )
        ]
    )
    assert any(i.code == SemanticIssueCode.QUOTE_NOT_FOUND for i in _validate(report))


def test_report_priorities_sequential() -> None:
    report = make_report(
        actions=[
            ReportAction(priority=1, related_turn_id=T2, text="a"),
            ReportAction(priority=3, related_turn_id=T1, text="b"),
        ]
    )
    assert any(i.code == SemanticIssueCode.PRIORITIES_NOT_SEQUENTIAL for i in _validate(report))


def test_report_action_must_be_linked_to_turn() -> None:
    report = make_report(
        actions=[ReportAction(priority=1, related_turn_id=None, text="Общее действие")]
    )
    codes = {i.code for i in _validate(report)}
    assert SemanticIssueCode.ACTION_NOT_LINKED_TO_TURN in codes


def test_report_action_must_link_a_gap_when_gaps_exist() -> None:
    report = make_report(
        actions=[
            ReportAction(priority=1, related_turn_id=T1, text="a"),
            ReportAction(priority=2, related_turn_id=T1, text="b"),
        ]
    )
    assert any(i.code == SemanticIssueCode.ACTION_NOT_LINKED_TO_GAP for i in _validate(report))


def test_report_empty_strengths_allowed_when_no_evidence() -> None:
    """При отсутствии достоверных сильных сторон список пуст (§3.4)."""
    report = make_report(strengths=[])
    assert _validate(report) == []
