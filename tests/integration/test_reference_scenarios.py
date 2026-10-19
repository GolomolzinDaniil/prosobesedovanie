"""Эталоны AT04/AT10 (§3.11; задача T21): 18 наборов роль × уровень × качество.

Каждый набор (tests/references/reference_sets.json) включает технический и
HR-ответ, ожидаемые рубрики оценок, допустимые переходы по фиксированным
правилам §3.3 и недопустимые выдуманные утверждения. Автоматическая часть
проверяет:
- переходы слабого/среднего/сильного ответа соответствуют правилам сервера
  (уточнение при тематической ≤2 с недостающими пунктами; шаг сложности
  после основного технического ответа; HR сложность не меняет; уточнение
  использует сложность родителя);
- отчёт по эталонным ответам не содержит выдуманных утверждений, действия
  конкретны (1–3), цитаты проверяемы;
- семантический валидатор отклоняет выдуманную цитату/опыт.

Предметная проверка самих наборов рецензентом (2–4 ч) — условие запуска;
автоматическая JSON-проверка участие рецензента не подменяет (§3.11).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from contracts.models import (
    Coverage,
    FeedbackReport,
    QuestionType,
    ReportAction,
    ReportFinding,
    SnapshotTurn,
    SuggestedAction,
    TurnScores,
)
from contracts.semantic_validation import (
    compute_allowed_action,
    compute_next_difficulty,
    validate_report,
)

REFERENCES = json.loads(
    (Path(__file__).resolve().parent.parent / "references" / "reference_sets.json")
    .read_text(encoding="utf-8")
)
SETS = REFERENCES["sets"]
SET_IDS = [s["id"] for s in SETS]

INITIAL_DIFFICULTY = {"intern": 1, "junior": 2}
DIFFICULTY_BOUNDS = {"intern": (1, 2), "junior": (1, 3)}


def _scores(ref_assessment: dict, question_type: str) -> TurnScores:
    scores: dict = {
        "relevance": ref_assessment["relevance"],
        "structure": ref_assessment["structure"],
        "correctness": None,
        "evidence": None,
    }
    if question_type == "technical":
        scores["correctness"] = ref_assessment.get("correctness", ref_assessment.get("topic"))
    else:
        scores["evidence"] = ref_assessment.get("evidence", ref_assessment.get("topic"))
    return TurnScores(**scores)


# ---------------------------------------------------------------------------
# AT04: допустимые переходы по фиксированным правилам §3.3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", SETS, ids=SET_IDS)
def test_reference_transitions_follow_fixed_rules(ref) -> None:
    level = ref["level"]
    lo, hi = DIFFICULTY_BOUNDS[level]
    initial = INITIAL_DIFFICULTY[level]

    for key, qtype in (("hr_self_introduction", "hr"), ("technical_main3", "technical")):
        assessment = ref["expected_assessment"][key]
        expected = ref["expected_transitions"][key]
        scores = _scores(assessment, qtype)
        action = compute_allowed_action(
            question_type=QuestionType(qtype),
            scores=scores,
            missing_points=assessment["missing_points"],
            follow_ups_remaining=2,
            parent_has_follow_up=False,
            answer_text=ref["answers"][key],
        )
        assert action.value == expected["action"], f"{ref['id']}/{key}: действие"

        next_difficulty = compute_next_difficulty(
            current=initial,
            difficulty_min=lo,
            difficulty_max=hi,
            question_type=QuestionType(qtype),
            is_main_answer=True,
            scores=scores,
        )
        if qtype == "hr":
            # HR техническую сложность не меняет (§3.3)
            assert next_difficulty == initial
            assert expected["difficulty_delta"] == 0
        else:
            assert next_difficulty == expected["next_difficulty"]
            assert lo <= next_difficulty <= hi


@pytest.mark.parametrize("ref", SETS, ids=SET_IDS)
def test_reference_dont_know_and_relevance_zero(ref) -> None:
    """Граничные правила: «не знаю» и relevance=0 — всегда следующая тема."""
    assessment = ref["expected_assessment"]["technical_main3"]
    scores = _scores(assessment, "technical")
    dont_know = compute_allowed_action(
        question_type=QuestionType.technical,
        scores=scores,
        missing_points=assessment["missing_points"],
        follow_ups_remaining=2,
        parent_has_follow_up=False,
        answer_text="Не знаю.",
    )
    assert dont_know is SuggestedAction.next_topic
    zero_relevance = compute_allowed_action(
        question_type=QuestionType.technical,
        scores=TurnScores(relevance=0, structure=2, correctness=1, evidence=None),
        missing_points=["пункт"],
        follow_ups_remaining=2,
        parent_has_follow_up=False,
        answer_text=ref["answers"]["technical_main3"],
    )
    assert zero_relevance is SuggestedAction.next_topic


@pytest.mark.parametrize("ref", SETS, ids=SET_IDS)
def test_reference_follow_up_limits(ref) -> None:
    """Уточнение невозможно при исчерпанном лимите или наличии уточнения
    у родителя (§1.3)."""
    assessment = ref["expected_assessment"]["technical_main3"]
    scores = _scores(assessment, "technical")
    for kwargs in (
        {"follow_ups_remaining": 0, "parent_has_follow_up": False},
        {"follow_ups_remaining": 1, "parent_has_follow_up": True},
    ):
        action = compute_allowed_action(
            question_type=QuestionType.technical,
            scores=scores,
            missing_points=assessment["missing_points"] or ["пункт"],
            answer_text=ref["answers"]["technical_main3"],
            **kwargs,
        )
        assert action is SuggestedAction.next_topic


# ---------------------------------------------------------------------------
# AT04 в движении: эталонные ответы через полный конвейер
# ---------------------------------------------------------------------------


def _catalog_fallback(config, role: str, level: str, topic: str) -> str:
    for scenario in config.catalog.scenarios:
        if scenario.role_id.value == role and scenario.level.value == level:
            for entry in scenario.entries:
                if entry.topic == topic:
                    return entry.fallback_question
    raise AssertionError(f"catalog entry not found: {role}/{level}/{topic}")


def _make_responder(ref: dict, harness):
    from tests.fakes import FakeLlm

    def responder(data: dict, call_no: int) -> str:
        if "snapshot_turns" in data:
            # операция отчёта: валидный FeedbackReport из фактического снимка
            return json.dumps(FakeLlm.default_report(data), ensure_ascii=False)
        answer = data["answer"]
        quote = answer.strip().split(".")[0][:80] or answer[:40]
        if data["kind"] == "follow_up":
            assessment = ref["expected_assessment"]["follow_up"]
            action = "next_topic"
        elif data["topic"] == "self_introduction":
            assessment = ref["expected_assessment"]["hr_self_introduction"]
            action = ref["expected_transitions"]["hr_self_introduction"]["action"]
        elif (
            data["question_type"] == "technical"
            and data["topic"] == ref["technical_topic"]
            and data["kind"] == "main"
        ):
            assessment = ref["expected_assessment"]["technical_main3"]
            action = ref["expected_transitions"]["technical_main3"]["action"]
        else:
            # прочие основные (project_story): нейтральная оценка — переход дальше
            assessment = {"relevance": 3, "structure": 3, "topic": 3, "missing_points": []}
            action = "next_topic"
        scores: dict = {
            "relevance": assessment["relevance"],
            "structure": assessment["structure"],
            "correctness": None,
            "evidence": None,
        }
        if data["question_type"] == "technical":
            scores["correctness"] = assessment.get("correctness", assessment.get("topic"))
        else:
            scores["evidence"] = assessment.get("evidence", assessment.get("topic"))
        next_question = None
        if action == "follow_up":
            # вопрос уточнения из каталога — гарантированно в теме (§3.3)
            next_question = _catalog_fallback(
                harness.config, data["role_id"], data["level"], data["topic"]
            )
        return json.dumps(
            {
                "turn_id": data["turn_id"],
                "scores": scores,
                "missing_points": assessment["missing_points"],
                "evidence_quotes": [quote] if quote.strip() else [],
                "explanation": "Эталонная оценка набора " + ref["id"],
                "suggested_action": action,
                "next_question": next_question,
            },
            ensure_ascii=False,
        )

    return responder


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", SETS, ids=SET_IDS)
async def test_reference_set_end_to_end(harness, ref) -> None:
    harness.llm.responder = _make_responder(ref, harness)
    await harness.complete_entry(role=ref["role_id"], level=ref["level"], fmt="text")
    session = await harness.fetch_active_session()
    initial = INITIAL_DIFFICULTY[ref["level"]]
    assert session["current_difficulty"] == initial

    async def current_turn():
        s = await harness.fetch_session(session["id"])
        turns = await harness.fetch_turns(session["id"])
        return s, next(t for t in turns if t["id"] == s["current_turn_id"])

    async def answer(text: str) -> None:
        _s, turn = await current_turn()
        await harness.send_text(text, reply_to_message_id=turn["delivered_message_id"])
        await harness.run_jobs()
        await harness.drain_outbox()

    # main1 — HR self_introduction: эталонный переход
    exp_hr = ref["expected_transitions"]["hr_self_introduction"]
    _s, main1 = await current_turn()
    await answer(ref["answers"]["hr_self_introduction"])
    session_row, turn = await current_turn()
    assert session_row["current_difficulty"] == initial  # HR не меняет
    if exp_hr["action"] == "follow_up":
        assert turn["kind"] == "follow_up"
        assert turn["topic"] == "self_introduction"
        # уточнение использует сложность родителя (у HR она NULL)
        assert turn["difficulty"] == main1["difficulty"]
        await answer(ref["answers"]["follow_up"])
        session_row, turn = await current_turn()
        assert session_row["current_difficulty"] == initial
    assert turn["kind"] == "main" and turn["main_index"] == 2

    # main2 — HR project_story: нейтральная оценка, переход к техническому
    await answer("Расскажу про учебный проект: собирал данные и делал выводы.")
    session_row, turn = await current_turn()
    assert turn["main_index"] == 3
    assert turn["question_type"] == "technical"
    assert turn["topic"] == ref["technical_topic"]
    main3 = turn

    # main3 — технический: эталонный переход и шаг сложности (§3.3)
    exp_tech = ref["expected_transitions"]["technical_main3"]
    await answer(ref["answers"]["technical_main3"])
    session_row, turn = await current_turn()
    assert session_row["current_difficulty"] == exp_tech["next_difficulty"]
    if exp_tech["action"] == "follow_up":
        assert turn["kind"] == "follow_up"
        assert turn["topic"] == ref["technical_topic"]
        assert turn["difficulty"] == main3["difficulty"]  # сложность родителя
        expected_used = 2 if exp_hr["action"] == "follow_up" else 1
        assert session_row["follow_ups_used"] == expected_used
        await answer(ref["answers"]["follow_up"])
        session_row, turn = await current_turn()
        # ответ на уточнение сложность не меняет
        assert session_row["current_difficulty"] == exp_tech["next_difficulty"]
        assert turn["kind"] == "main" and turn["main_index"] == 4
    else:
        assert turn["kind"] == "main" and turn["main_index"] == 4
        assert turn["difficulty"] == exp_tech["next_difficulty"]

    # Раннее завершение -> отчёт по эталонным ответам
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"

    report = await harness.fetch_report(session["id"])
    assert report is not None
    assert report["degraded"] is False  # AT10: эталоны не должны деградировать
    data = report["report_json"]
    if isinstance(data, str):
        data = json.loads(data)
    constraints = ref["report_constraints"]
    assert constraints["actions_min"] <= len(data["actions"]) <= constraints["actions_max"]
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    text = "\n".join(parts).lower()
    for claim in constraints["forbidden_claims"]:
        assert claim.lower() not in text, f"выдуманное утверждение в отчёте: {claim}"


# ---------------------------------------------------------------------------
# AT10: выдуманные утверждения отклоняются семантическим валидатором
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", SETS, ids=SET_IDS)
def test_fabricated_experience_rejected_by_validator(ref) -> None:
    """Цитата, которой нет в ответах (выдуманное утверждение), отклоняется:
    отчёт с таким доказательством не проходит семантическую валидацию
    (AT10: нет выдуманного опыта)."""
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    snapshot = [
        SnapshotTurn(
            turn_id=turn_id,
            ordinal=1,
            kind="main",
            question_type="technical",
            main_index=3,
            topic=ref["technical_topic"],
            question="Эталонный вопрос темы " + ref["technical_topic"] + "?",
            answer_text=ref["answers"]["technical_main3"],
            skipped=False,
            assessment=None,
        )
    ]
    fabricated = ref["report_constraints"]["forbidden_claims"][0]
    report = FeedbackReport(
        session_id=session_id,
        partial=False,
        coverage=Coverage(
            main_total=6, main_answered=1, main_skipped=5, follow_ups=0
        ),
        strengths=[],
        gaps=[ReportFinding(turn_id=turn_id, quote=fabricated, comment=fabricated)],
        actions=[
            ReportAction(
                priority=1,
                related_turn_id=turn_id,
                text="Конкретное действие с измеримым результатом за две недели.",
            )
        ],
        improved_examples=[],
    )
    issues = validate_report(
        report,
        snapshot=snapshot,
        expected_session_id=session_id,
        expected_partial=False,
    )
    assert issues, "выдуманная цитата обязана отклоняться валидатором"
    codes = {i.code for i in issues}
    assert "QUOTE_NOT_FOUND" in codes
