"""Валидность каталога вопросов (§3.3, задача T04): 6 сценариев, 36 записей."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Catalog, Settings, validate_catalog
from contracts.models import Level, QuestionType, RoleId


@pytest.fixture(scope="module")
def settings(settings_raw: dict[str, Any]) -> Settings:
    return Settings.model_validate(settings_raw)


@pytest.fixture(scope="module")
def catalog(catalog_raw: dict[str, Any]) -> Catalog:
    return Catalog.model_validate(catalog_raw)


def test_real_catalog_is_valid(catalog: Catalog, settings: Settings) -> None:
    assert validate_catalog(catalog, settings) == []


def test_catalog_version_and_shape(catalog: Catalog) -> None:
    assert catalog.version == "2.0"
    assert len(catalog.scenarios) == 6
    assert sum(len(s.entries) for s in catalog.scenarios) == 36
    pairs = {(s.role_id, s.level) for s in catalog.scenarios}
    assert pairs == {(r, lv) for r in RoleId for lv in Level}


def test_topics_are_prescribed_per_role(catalog: Catalog, settings: Settings) -> None:
    """Python: коллекции, исключения, область видимости, поиск/обработка данных;
    аналитик: SQL-фильтрация, JOIN, качество данных, метрики;
    QA: тест-дизайн, баг-репорт, приоритет, API (§3.3)."""
    assert catalog.technical_topics[RoleId.python] == [
        "collections",
        "exceptions",
        "scoping",
        "data_processing",
    ]
    assert catalog.technical_topics[RoleId.analyst] == [
        "sql_filtering",
        "sql_joins",
        "data_quality",
        "metrics",
    ]
    assert catalog.technical_topics[RoleId.qa] == [
        "test_design",
        "bug_report",
        "prioritization",
        "api_testing",
    ]
    for scenario in catalog.scenarios:
        entries = scenario.entries
        # Сначала самопрезентация и рассказ об учебном/личном проекте
        assert entries[0].topic == "self_introduction"
        assert entries[1].topic == "project_story"
        assert entries[0].question_type is QuestionType.hr
        assert entries[1].question_type is QuestionType.hr
        technical = entries[2:]
        assert [e.topic for e in technical] == catalog.technical_topics[scenario.role_id]
        assert all(e.question_type is QuestionType.technical for e in technical)
        assert [e.main_index for e in entries] == [1, 2, 3, 4, 5, 6]


def test_every_entry_has_rubric_fallback_and_advice(catalog: Catalog) -> None:
    for scenario in catalog.scenarios:
        for entry in scenario.entries:
            assert entry.rubric.key_points, entry.id
            assert entry.rubric.score_guidance.s0 and entry.rubric.score_guidance.s4
            assert entry.fallback_question.strip(), entry.id
            assert entry.advice.strip(), entry.id
            if entry.question_type is QuestionType.hr:
                assert entry.rubric.evidence_criteria, entry.id
            else:
                assert entry.rubric.evidence_criteria is None, entry.id


def test_difficulty_variants_cover_level_range(catalog: Catalog) -> None:
    """intern ограничен 1–2, junior 1–3 (§3.3)."""
    for scenario in catalog.scenarios:
        for entry in scenario.entries:
            if entry.question_type is not QuestionType.technical:
                continue
            variants = set((entry.difficulty_variants or {}).keys())
            if scenario.level is Level.intern:
                assert variants == {"1", "2"}, entry.id
            else:
                assert variants == {"1", "2", "3"}, entry.id


def test_question_for_difficulty_resolution(catalog: Catalog) -> None:
    junior_python = next(
        s for s in catalog.scenarios if s.role_id is RoleId.python and s.level is Level.junior
    )
    collections_entry = junior_python.entries[2]
    variants = collections_entry.difficulty_variants or {}
    assert collections_entry.question_for_difficulty(1) == variants["1"]
    assert collections_entry.question_for_difficulty(3) == variants["3"]
    # HR-запись всегда возвращает свой вопрос
    hr = junior_python.entries[0]
    assert hr.question_for_difficulty(2) == hr.question


def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(raw)


def test_broken_entry_count_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    broken["scenarios"][0]["entries"].pop()
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("35 записей вместо 36" in p or "записей вместо" in p for p in problems), problems


def test_missing_scenario_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    removed = broken["scenarios"].pop()
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("отсутствуют сценарии" in p for p in problems)
    assert any(removed["role_id"] in p for p in problems)


def test_duplicate_scenario_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    broken["scenarios"].append(copy.deepcopy(broken["scenarios"][0]))
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("дублирующийся сценарий" in p for p in problems)


def test_wrong_topic_order_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    entries = broken["scenarios"][0]["entries"]
    entries[2], entries[3] = entries[3], entries[2]
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("topic=" in p for p in problems)


def test_overlong_question_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    entry = broken["scenarios"][0]["entries"][2]
    entry["difficulty_variants"]["1"] = "в" * 701
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("длина формулировки" in p for p in problems)


def test_hr_without_evidence_criteria_rejected(catalog_raw: dict[str, Any]) -> None:
    broken = _mutate(catalog_raw)
    broken["scenarios"][0]["entries"][0]["rubric"].pop("evidence_criteria")
    with pytest.raises(ValidationError):
        Catalog.model_validate(broken)


def test_technical_without_variants_rejected(catalog_raw: dict[str, Any]) -> None:
    broken = _mutate(catalog_raw)
    broken["scenarios"][0]["entries"][2].pop("difficulty_variants")
    with pytest.raises(ValidationError):
        Catalog.model_validate(broken)


def test_missing_difficulty_variant_detected(
    catalog_raw: dict[str, Any], settings: Settings
) -> None:
    broken = _mutate(catalog_raw)
    # у junior-сценария (индекс 1) убираем вариант сложности 3
    broken["scenarios"][1]["entries"][2]["difficulty_variants"].pop("3")
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("не покрывает диапазон уровня" in p for p in problems)


def test_duplicate_entry_id_detected(catalog_raw: dict[str, Any], settings: Settings) -> None:
    broken = _mutate(catalog_raw)
    broken["scenarios"][0]["entries"][1]["id"] = broken["scenarios"][0]["entries"][0]["id"]
    problems = validate_catalog(Catalog.model_validate(broken), settings)
    assert any("дублирующийся id" in p for p in problems)
