"""Startup-валидация settings.json (§3.6): инварианты и несогласованности."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, validate_settings_consistency
from contracts.models import ErrorCode


def _settings(raw: dict[str, Any]) -> Settings:
    return Settings.model_validate(raw)


def test_real_settings_are_consistent(settings_raw: dict[str, Any]) -> None:
    problems = validate_settings_consistency(_settings(settings_raw))
    assert problems == []


def test_real_settings_key_values(settings_raw: dict[str, Any]) -> None:
    """Фиксированные значения проекта (§1.1, §1.3)."""
    s = _settings(settings_raw)
    assert s.version == "2.0"
    assert [r.value for r in s.roles] == ["python", "analyst", "qa"]
    assert [lv.value for lv in s.levels] == ["intern", "junior"]
    assert [f.value for f in s.formats] == ["text", "voice"]
    assert s.interview.main_questions == 6
    assert s.interview.hr_questions == 2
    assert s.interview.technical_questions == 4
    assert s.interview.max_follow_ups_per_main == 1
    assert s.interview.max_follow_ups_per_session == 2
    assert s.interview.max_turns == 8
    assert s.limits.answer_max_chars == 4000
    assert s.limits.question_max_chars == 700
    assert s.limits.voice_max_bytes == 10 * 1024 * 1024
    assert s.limits.voice_max_seconds == 120
    assert s.limits.report_max_chars == 12000
    assert s.limits.message_part_max_chars == 3500
    assert s.limits.webhook_body_max_bytes == 1024 * 1024
    assert s.limits.starts_per_24h == 3
    assert s.limits.commands_per_minute == 10
    assert s.limits.concurrent_llm == 5
    assert s.limits.concurrent_stt == 2
    assert s.limits.concurrent_tts == 2
    assert s.tokens_budget.model_context_min >= 16000
    assert s.timeouts_seconds.worker_lease == 120
    assert s.timeouts_seconds.worker_heartbeat == 20
    assert s.flags.billing_enabled is False
    assert s.flags.voice_enabled is True
    assert s.interview.llm_temperature == pytest.approx(0.2)


def _mutate(raw: dict[str, Any], path: list[str], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(raw)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


# (путь, значение, подстрока ожидаемой проблемы)
BROKEN_INVARIANTS = [
    (["interview", "max_turns"], 7, "max_turns"),
    (["interview", "hr_questions"], 3, "hr_questions + technical_questions"),
    (["limits", "telegram_message_max_chars"], 5000, "предел Bot API 4096"),
    (["limits", "message_part_max_chars"], 4097, "message_part_max_chars больше"),
    (["limits", "report_parts_max"], 2, "части отчёта не покрывают"),
    (["limits", "callback_data_max_bytes"], 65, "предел Bot API 64"),
    (["limits", "ui_token_length_chars"], 24, "22"),
    (["limits", "answer_max_chars"], 5000, "1..4000"),
    (["limits", "question_max_chars"], 701, "700"),
    (["limits", "voice_max_seconds"], 121, "10 MiB и 120"),
    (["limits", "stt_attempts_per_turn"], 4, "не более 3"),
    (["limits", "max_provider_calls_per_operation"], 3, "обязан быть 2"),
    (["limits", "provider_retry_pause_min_seconds"], 6, "min > max"),
    (["tokens_budget", "report_reserve_input"], 100001, "report_reserve_input"),
    (["tokens_budget", "report_reserve_output"], 22001, "report_reserve_output"),
    (["tokens_budget", "model_context_min"], 15000, "16000"),
    (["tokens_budget", "assess_output_max"], 5000, "model_context_min"),
    (["timeouts_seconds", "worker_heartbeat"], 120, "worker_heartbeat >= worker_lease"),
    (["timeouts_seconds", "assess_call"], 111, "assessment_deadline_total"),
    (["timeouts_seconds", "report_call"], 141, "report_deadline_total"),
    (["timeouts_seconds", "stt_call"], 140, "transcription_deadline_total"),
    (["timeouts_seconds", "tts_call"], 96, "tts_deadline_total"),
    (["limits", "ai_queue_max_seconds"], 95, "ai_queue_max_seconds + assess_call"),
    (["slo_p95_seconds", "next_text_question"], 110, "SLO next_text_question"),
    (["slo_p95_seconds", "text_report"], 140, "SLO text_report"),
    (["slo_p95_seconds", "transcription"], 150, "SLO transcription"),
    (["slo_p95_seconds", "voiced_question"], 95, "SLO voiced_question"),
    (["slo_p95_seconds", "first_text_question"], 16, "first_text_question > next"),
    (["retention", "session_inactive_expire_hours"], 25, "24"),
    (["retention", "audio_max_hours"], 2, "не более 1"),
    (["retention", "completed_content_days"], 31, "30"),
    (["retention", "tombstone_days"], 8, "7"),
    (["retention", "deletion_deadline_hours"], 25, "не более 24"),
    (["sender", "max_retries"], 11, "не более 10"),
    (["sender", "retry_window_hours"], 25, "не более 24"),
    (["inbox", "max_handler_retries"], 6, "не более 5"),
    (["alerts", "worker_heartbeat_missing_seconds"], 10, "worker_heartbeat"),
    (["language"], "en", "рабочий язык проекта — ru"),
]


@pytest.mark.parametrize(("path", "value", "expected"), BROKEN_INVARIANTS)
def test_invariant_violations_detected(
    settings_raw: dict[str, Any], path: list[str], value: Any, expected: str
) -> None:
    problems = validate_settings_consistency(_settings(_mutate(settings_raw, path, value)))
    assert any(expected in problem for problem in problems), problems


def test_roles_fixed_not_free_text(settings_raw: dict[str, Any]) -> None:
    """Решения фиксируются в конфигурации, а не свободным текстом (§1.1)."""
    broken = _mutate(settings_raw, ["roles"], ["python", "designer"])
    with pytest.raises(ValidationError):
        _settings(broken)  # неизвестная роль отклоняется перечислением


def test_unknown_settings_field_rejected(settings_raw: dict[str, Any]) -> None:
    broken = copy.deepcopy(settings_raw)
    broken["limits"]["answer_max_symbols_typo"] = 10
    with pytest.raises(ValidationError):
        _settings(broken)


def test_error_registry_covers_all_codes() -> None:
    """Каждый код реестра §3.2 имеет сообщение и retryable-признак."""
    from app.domain.errors import ERROR_REGISTRY

    for code in ErrorCode:
        assert code in ERROR_REGISTRY, f"{code.value} без сообщения в реестре"
        message, _ = ERROR_REGISTRY[code]
        assert message and isinstance(message, str)


def test_rate_limited_requires_retry_after() -> None:
    from app.domain.errors import DomainError

    with pytest.raises(ValueError):
        DomainError(ErrorCode.RATE_LIMITED)
    err = DomainError(ErrorCode.RATE_LIMITED, retry_after_seconds=30)
    body = err.body()
    assert body.retryable is True
    assert body.retry_after_seconds == 30
