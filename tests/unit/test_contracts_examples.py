"""Проверка нормативных примеров contracts/examples по manifest.json (§5.1).

Каждый пример проверяется на своём этапе:
- structural — нормативная модель Pydantic (extra=forbid, диапазоны);
- domain — доменная валидация (граничные длины ответа);
- semantic — contracts/semantic_validation.py (цитаты, ссылки, приоритеты).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import contracts.models as models
from app.domain.answers import validate_answer_text
from app.domain.errors import DomainError, map_all_validation_errors
from contracts.models import SnapshotTurn
from contracts.semantic_validation import validate_assessment, validate_report

EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "contracts" / "examples"
MANIFEST = json.loads((EXAMPLES_DIR / "manifest.json").read_text(encoding="utf-8"))


def _load(name: str) -> Any:
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def _model(name: str) -> type:
    return getattr(models, name)


def _example_ids() -> list[str]:
    return [e["file"] for e in MANIFEST["examples"]]


@pytest.fixture(params=_example_ids())
def example(request: pytest.FixtureRequest) -> dict[str, Any]:
    entry = next(e for e in MANIFEST["examples"] if e["file"] == request.param)
    return entry


def test_manifest_is_versioned() -> None:
    assert MANIFEST["version"] == "2.0"
    assert len(MANIFEST["examples"]) >= 15


def test_example_file_exists(example: dict[str, Any]) -> None:
    assert (EXAMPLES_DIR / example["file"]).is_file()


def test_example_validity(example: dict[str, Any]) -> None:
    payload = _load(example["file"])
    stage = example["stage"]
    model = _model(example["model"])

    if stage == "structural":
        _check_structural(example, payload, model)
    elif stage == "domain":
        _check_domain(example, payload, model)
    elif stage == "semantic":
        _check_semantic(example, payload, model)
    else:  # pragma: no cover
        pytest.fail(f"неизвестный stage {stage}")


def _check_structural(example: dict[str, Any], payload: Any, model: type) -> None:
    if example["valid"]:
        model.model_validate(payload)
        return
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(payload)
    expected_codes = set(example.get("expected_error_codes", []))
    if expected_codes:
        mapped = map_all_validation_errors(exc_info.value, ui_selection=True)
        actual = {e.code.value for e in mapped}
        assert expected_codes <= actual, (expected_codes, actual)
    expected_field = example.get("expected_field")
    if expected_field:
        mapped = map_all_validation_errors(exc_info.value)
        assert any(e.field == expected_field for e in mapped), [e.field for e in mapped]


def _check_domain(example: dict[str, Any], payload: Any, model: type) -> None:
    request = model.model_validate(payload)  # структурно пример всегда корректен
    if example["valid"]:
        validate_answer_text(request.text)
        return
    with pytest.raises(DomainError) as exc_info:
        validate_answer_text(request.text)
    assert exc_info.value.code.value in example["expected_error_codes"]


def _check_semantic(example: dict[str, Any], payload: Any, model: type) -> None:
    parsed = model.model_validate(payload)
    context = example.get("semantic_context", {})
    if model is models.TurnAssessment:
        issues = validate_assessment(
            parsed,
            question_type=models.QuestionType(context["question_type"]),
            answer_text=context["answer_text"],
        )
    elif model is models.FeedbackReport:
        snapshot = [SnapshotTurn.model_validate(t) for t in context["snapshot"]]
        issues = validate_report(
            parsed,
            snapshot=snapshot,
            expected_session_id=parsed.session_id,
            expected_partial=context["expected_partial"],
        )
    else:  # pragma: no cover
        pytest.fail(f"семантический этап не определён для {model.__name__}")

    codes = {i.code for i in issues}
    if example["valid"]:
        assert not issues, [(i.code, i.message) for i in issues]
    else:
        assert set(example["expected_error_codes"]) <= codes, (
            example["expected_error_codes"],
            codes,
        )


def test_successful_payment_example_is_75_xtr() -> None:
    """Пример 75 XTR служит проверке формата и не устанавливает цену (§3.8)."""
    payload = _load("successful-payment.json")
    assert payload["currency"] == "XTR"
    assert payload["total_amount"] == 75
    models.SuccessfulPaymentPayload.model_validate(payload)
