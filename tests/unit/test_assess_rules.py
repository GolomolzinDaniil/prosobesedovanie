"""Правила адаптации и разбор вывода модели (§3.3): юнит-проверки."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.config import Catalog
from app.services.assess_service import (
    build_generate_turn_input,
    parse_assessment_json,
    question_matches_topic,
    validate_assessment_payload,
)
from contracts.models import QuestionType


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    return Catalog.model_validate_json((root / "config" / "questions.json").read_text("utf-8"))


def _entry(catalog: Catalog, role: str, level: str, main_index: int):
    scenario = next(
        s
        for s in catalog.scenarios
        if s.role_id.value == role and s.level.value == level
    )
    return next(e for e in scenario.entries if e.main_index == main_index)


# ---------------------------------------------------------------------------
# Тематическая эвристика следующего вопроса (§3.3)
# ---------------------------------------------------------------------------


def test_on_topic_question_accepted(catalog: Catalog) -> None:
    entry = _entry(catalog, "python", "junior", 4)  # exceptions
    assert entry.topic == "exceptions"
    question = (
        "Какие блоки try/except/else/finally вы знаете и когда нужно "
        "перехватывать конкретные классы исключений?"
    )
    assert question_matches_topic(question, entry)


def test_off_topic_question_rejected(catalog: Catalog) -> None:
    entry = _entry(catalog, "python", "junior", 3)  # collections
    question = "Объясните разницу между INNER JOIN и LEFT JOIN в SQL-запросах."
    assert not question_matches_topic(question, entry)


def test_analyst_metrics_question_accepted(catalog: Catalog) -> None:
    entry = _entry(catalog, "analyst", "junior", 6)  # metrics
    question = (
        "Как построить когорту для расчёта retention и выбрать окно наблюдения "
        "для метрики удержания?"
    )
    assert question_matches_topic(question, entry)


def test_empty_question_rejected(catalog: Catalog) -> None:
    entry = _entry(catalog, "qa", "intern", 3)
    assert not question_matches_topic("", entry)
    assert not question_matches_topic("?", entry)


# ---------------------------------------------------------------------------
# Разбор JSON вывода модели (§3.3: строгий разбор одного объекта)
# ---------------------------------------------------------------------------


def test_parse_plain_json() -> None:
    assert parse_assessment_json('{"a": 1}') == {"a": 1}


def test_parse_fenced_json() -> None:
    assert parse_assessment_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        parse_assessment_json("[1, 2]")


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_assessment_json("это не json")


def test_validate_payload_turn_id_mismatch() -> None:
    turn_id = uuid4()
    data = {
        "turn_id": str(uuid4()),
        "scores": {"relevance": 3, "structure": 3, "correctness": 3, "evidence": None},
        "missing_points": [],
        "evidence_quotes": ["ответ"],
        "explanation": "норм",
        "suggested_action": "next_topic",
        "next_question": None,
    }
    with pytest.raises(ValueError, match="turn_id"):
        validate_assessment_payload(
            data,
            expected_turn_id=turn_id,
            question_type=QuestionType.technical,
            answer_text="ответ пользователя",
        )


# ---------------------------------------------------------------------------
# Сокращение истории (§3.3: по старым объяснениям, не по текущему ответу)
# ---------------------------------------------------------------------------


class _Row(dict):
    """Псевдо-запись asyncpg для юнит-теста сборки входа."""

    def __getitem__(self, key):
        return dict.get(self, key)


def _make_ctx(previous_count: int, explanation_len: int, missing_len: int = 10):
    turn_id = uuid4()
    session_id = uuid4()
    turn = _Row(
        id=turn_id,
        session_id=session_id,
        ordinal=previous_count + 1,
        kind="main",
        question_type="technical",
        main_index=previous_count + 1,
        parent_turn_id=None,
        topic="collections",
        question_text="Вопрос про коллекции?",
        rubric={"key_points": ["пункт"]},
        difficulty=2,
        catalog_entry_id="python-junior-tech-collections",
        answer_text="ТЕКУЩИЙ ОТВЕТ, который нельзя сокращать " + "детали " * 10,
        skipped=False,
    )
    previous = []
    for i in range(previous_count):
        previous.append(
            _Row(
                id=uuid4(),
                topic=f"topic{i}",
                assessment=json.dumps(
                    {
                        "turn_id": str(uuid4()),
                        "scores": {
                            "relevance": 3,
                            "structure": 3,
                            "correctness": 3,
                            "evidence": None,
                        },
                        "missing_points": ["п" * missing_len],
                        "explanation": "о" * explanation_len,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    session = _Row(
        id=session_id,
        role_id="python",
        level="junior",
        follow_ups_used=0,
        current_difficulty=2,
    )
    user = _Row(id=uuid4(), chat_id=555)
    from app.services.assess_service import AssessContext

    return AssessContext(user=user, session=session, turn=turn, previous=previous,
                         next_entry=None)


def test_history_truncated_keeping_current_answer() -> None:
    from app.config import load_config
    from app.services.token_budget import estimate_tokens
    from tests.conftest import make_env

    config = load_config(make_env())
    # максимум 7 предыдущих оценок (до 8 turns), но с длинными объяснениями
    # они не помещаются в 12000 токенов -> сокращение по старым объяснениям
    ctx = _make_ctx(previous_count=7, explanation_len=300, missing_len=4200)
    data, system_prompt, user_payload = build_generate_turn_input(config, ctx)

    assert estimate_tokens(system_prompt + user_payload) <= (
        config.settings.tokens_budget.assess_input_max
    )
    # текущий ответ не сокращается (§3.3)
    assert ctx.turn["answer_text"] in user_payload
    # история сокращена: самые старые оценки выброшены целиком
    assert len(data.previous_assessments) < 7
    # сокращение по старым объяснениям: у оставшихся объяснения удалены первыми
    assert all(b.brief_explanation is None for b in data.previous_assessments)
