"""Загрузка и startup-валидация конфигурации (§3.6).

На startup проверяются:
- max_turns = main_questions + max_follow_ups_per_session;
- лимиты частей Telegram (часть <= 4096, части отчёта покрывают 12000,
  callback_data <= 64 байт, ui-токен 22 символа);
- резерв отчёта меньше бюджета сессии;
- интервалы heartbeat меньше lease;
- целевые (SLO) и аварийные времена согласованы;
- валидность каталога (6 сценариев, 36 записей, темы по ролям, длины);
- наличие обязательных секретов; route_token не содержит токен бота;
- products: billing не включается без положительных XTR-цен;
- provider-profile: null-значения означают незаполненную внешнюю зависимость
  и блокируют AI-вызовы (не webhook).

Settings не содержит модели и ключи. Конфигурации версионируются в репозитории;
при изменении каталога/профиля/промпта создаётся новая версия, активная сессия
продолжает использовать зафиксированную.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from contracts.models import InterviewFormat, Level, QuestionType, RoleId

logger = logging.getLogger(__name__)

CONFIG_VERSION = "2.0"

HR_TOPIC_SELF_INTRO = "self_introduction"
HR_TOPIC_PROJECT = "project_story"
HR_TOPICS_ORDERED = [HR_TOPIC_SELF_INTRO, HR_TOPIC_PROJECT]


class ConfigError(Exception):
    """Некорректная конфигурация: сервис не готов (503 ready), запуск блокируется."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Конфигурация некорректна: " + "; ".join(problems))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------


class InterviewSettings(_Strict):
    main_questions: int = Field(ge=1)
    hr_questions: int = Field(ge=0)
    technical_questions: int = Field(ge=1)
    max_follow_ups_per_main: int = Field(ge=0)
    max_follow_ups_per_session: int = Field(ge=0)
    max_turns: int = Field(ge=1)
    initial_difficulty: dict[Level, int]
    difficulty_min: dict[Level, int]
    difficulty_max: dict[Level, int]
    difficulty_up_min_topic_score: int = Field(ge=0, le=4)
    difficulty_up_min_relevance: int = Field(ge=0, le=4)
    difficulty_down_max_topic_score: int = Field(ge=0, le=4)
    llm_temperature: float = Field(ge=0, le=2)


class LimitsSettings(_Strict):
    answer_min_chars: int = Field(ge=1)
    answer_max_chars: int = Field(ge=1)
    question_min_chars: int = Field(ge=1)
    question_max_chars: int = Field(ge=1)
    telegram_message_max_chars: int = Field(ge=1)
    message_part_max_chars: int = Field(ge=1)
    report_max_chars: int = Field(ge=1)
    report_parts_min: int = Field(ge=1)
    report_parts_max: int = Field(ge=1)
    callback_data_max_bytes: int = Field(ge=1)
    ui_token_length_chars: int = Field(ge=1)
    webhook_body_max_bytes: int = Field(ge=1)
    active_sessions_per_user: int = Field(ge=1)
    starts_per_24h: int = Field(ge=1)
    commands_per_minute: int = Field(ge=1)
    voice_max_bytes: int = Field(ge=1)
    voice_max_seconds: int = Field(ge=1)
    voice_format: str
    stt_attempts_per_turn: int = Field(ge=1)
    max_provider_calls_per_operation: int = Field(ge=1)
    provider_retry_pause_min_seconds: int = Field(ge=0)
    provider_retry_pause_max_seconds: int = Field(ge=0)
    concurrent_active_sessions_total: int = Field(ge=1)
    concurrent_llm: int = Field(ge=1)
    concurrent_stt: int = Field(ge=1)
    concurrent_tts: int = Field(ge=1)
    ai_queue_max_seconds: int = Field(ge=1)
    media_total_max_mib: int = Field(ge=1)
    media_file_name_chars: int = Field(ge=8)
    media_file_mode: str
    media_dir_mode: str
    decoder_memory_max_mib: int = Field(ge=1)
    decoder_cpu_seconds_max: int = Field(ge=1)
    decoder_wall_timeout_seconds: int = Field(ge=1)


class TokensBudgetSettings(_Strict):
    assess_input_max: int = Field(ge=1)
    assess_output_max: int = Field(ge=1)
    report_input_max: int = Field(ge=1)
    report_output_max: int = Field(ge=1)
    session_input_budget: int = Field(ge=1)
    session_output_budget: int = Field(ge=1)
    report_reserve_input: int = Field(ge=0)
    report_reserve_output: int = Field(ge=0)
    model_context_min: int = Field(ge=1)


class TimeoutsSettings(_Strict):
    assess_call: int = Field(ge=1)
    report_call: int = Field(ge=1)
    stt_call: int = Field(ge=1)
    tts_call: int = Field(ge=1)
    media_download: int = Field(ge=1)
    media_decode: int = Field(ge=1)
    assessment_deadline_total: int = Field(ge=1)
    report_deadline_total: int = Field(ge=1)
    transcription_deadline_total: int = Field(ge=1)
    tts_deadline_total: int = Field(ge=1)
    worker_lease: int = Field(ge=1)
    worker_heartbeat: int = Field(ge=1)
    pre_checkout_answer: int = Field(ge=1)


class SloSettings(_Strict):
    action_ack: int = Field(ge=1)
    first_text_question: int = Field(ge=1)
    next_text_question: int = Field(ge=1)
    text_report: int = Field(ge=1)
    transcription: int = Field(ge=1)
    voiced_question: int = Field(ge=1)


class RetentionSettings(_Strict):
    session_inactive_expire_hours: int = Field(ge=1)
    cancelled_content_removal_hours: int = Field(ge=1)
    completed_content_days: int = Field(ge=1)
    audio_max_hours: int = Field(ge=1)
    audio_sweep_interval_minutes: int = Field(ge=1)
    session_sweep_interval_minutes: int = Field(ge=1)
    retention_sweep_interval_minutes: int = Field(ge=1)
    stt_draft_max_hours: int = Field(ge=1)
    queue_payload_nullify_hours: int = Field(ge=1)
    tombstone_days: int = Field(ge=1)
    deletion_deadline_hours: int = Field(ge=1)
    queue_metadata_days: int = Field(ge=1)
    technical_logs_days: int = Field(ge=1)
    events_days: int = Field(ge=1)
    ai_calls_days: int = Field(ge=1)
    backup_rolling_days: int = Field(ge=1)


class UiTokenTtlSettings(_Strict):
    parameter_selection_minutes: int = Field(ge=1)
    delete_confirm_minutes: int = Field(ge=1)
    cancel_confirm_minutes: int = Field(ge=1)
    question_control_hours: int = Field(ge=1)


class UiSettings(_Strict):
    loading_update_after_seconds: int = Field(ge=1)
    loading_update_max_times: int = Field(ge=0)


class InboxSettings(_Strict):
    max_handler_retries: int = Field(ge=1)


class SenderSettings(_Strict):
    per_chat_per_second: int = Field(ge=1)
    global_per_second: int = Field(ge=1)
    max_retries: int = Field(ge=1)
    retry_window_hours: int = Field(ge=1)
    backoff_base_seconds: int = Field(ge=1)
    respect_retry_after: bool


class CostsSettings(_Strict):
    daily_ai_budget_usd: float | None = Field(default=None, ge=0)
    llm_input_per_1k_usd: float | None = Field(default=None, ge=0)
    llm_output_per_1k_usd: float | None = Field(default=None, ge=0)
    stt_per_minute_usd: float | None = Field(default=None, ge=0)
    tts_per_1k_chars_usd: float | None = Field(default=None, ge=0)
    budget_low_remainder_alert_ratio: float = Field(gt=0, le=1)


class AlertsSettings(_Strict):
    oldest_ai_job_seconds: int = Field(ge=1)
    degraded_report_ratio_max: float = Field(gt=0, le=1)
    degraded_report_window: int = Field(ge=1)
    worker_heartbeat_missing_seconds: int = Field(ge=1)


class FlagsSettings(_Strict):
    billing_enabled: bool
    voice_enabled: bool


class Settings(_Strict):
    version: str
    language: str
    roles: list[RoleId]
    levels: list[Level]
    formats: list[InterviewFormat]
    interview: InterviewSettings
    limits: LimitsSettings
    tokens_budget: TokensBudgetSettings
    timeouts_seconds: TimeoutsSettings
    slo_p95_seconds: SloSettings
    retention: RetentionSettings
    ui_tokens: UiTokenTtlSettings
    ui: UiSettings
    inbox: InboxSettings
    sender: SenderSettings
    costs: CostsSettings
    alerts: AlertsSettings
    flags: FlagsSettings
    support_contact: str | None = None
    campaign_source_allowlist: list[str] = Field(default_factory=list)


def validate_settings_consistency(s: Settings) -> list[str]:
    """Инварианты §3.6 + производственные диапазоны. Возвращает список проблем."""
    p: list[str] = []
    iv, lim, tb, to, slo, ret, send = (
        s.interview,
        s.limits,
        s.tokens_budget,
        s.timeouts_seconds,
        s.slo_p95_seconds,
        s.retention,
        s.sender,
    )

    if s.version != CONFIG_VERSION:
        p.append(f"settings.version={s.version}, ожидается {CONFIG_VERSION}")

    # max_turns = main_questions + max_follow_ups (§3.6)
    if iv.max_turns != iv.main_questions + iv.max_follow_ups_per_session:
        p.append(
            f"max_turns({iv.max_turns}) != main_questions({iv.main_questions}) "
            f"+ max_follow_ups_per_session({iv.max_follow_ups_per_session})"
        )
    if iv.hr_questions + iv.technical_questions != iv.main_questions:
        p.append("hr_questions + technical_questions != main_questions")
    if iv.max_follow_ups_per_main * iv.main_questions < iv.max_follow_ups_per_session:
        p.append("max_follow_ups_per_session недостижим при лимите на основной вопрос")

    # Лимиты Telegram и частей отчёта (§3.6)
    if lim.telegram_message_max_chars > 4096:
        p.append("telegram_message_max_chars превышает предел Bot API 4096")
    if lim.message_part_max_chars > lim.telegram_message_max_chars:
        p.append("message_part_max_chars больше telegram_message_max_chars")
    if lim.report_parts_max * lim.message_part_max_chars < lim.report_max_chars:
        p.append("части отчёта не покрывают report_max_chars")
    if lim.callback_data_max_bytes > 64:
        p.append("callback_data_max_bytes превышает предел Bot API 64")
    if lim.ui_token_length_chars != 22:
        p.append("ui_token_length_chars обязан быть 22 (16 байт base64url без padding)")
    if lim.answer_min_chars != 1 or lim.answer_max_chars != 4000:
        p.append("границы ответа обязаны быть 1..4000 (§1.3)")
    if lim.question_max_chars != 700:
        p.append("question_max_chars обязан быть 700 (§1.3)")
    if lim.voice_max_bytes != 10 * 1024 * 1024 or lim.voice_max_seconds != 120:
        p.append("пределы voice обязаны быть 10 MiB и 120 секунд (§1.3)")
    if lim.voice_format != "ogg_opus":
        p.append("voice_format обязан быть ogg_opus")
    if lim.stt_attempts_per_turn > 3:
        p.append("stt_attempts_per_turn не более 3 (§3.5)")
    if lim.max_provider_calls_per_operation != 2:
        p.append("max_provider_calls_per_operation обязан быть 2 (§3.2)")
    if lim.provider_retry_pause_min_seconds > lim.provider_retry_pause_max_seconds:
        p.append("пауза повтора провайдера: min > max")

    # Границы сложности по уровням (§3.3)
    for level in Level:
        lo, hi = iv.difficulty_min[level], iv.difficulty_max[level]
        init = iv.initial_difficulty[level]
        if not lo <= init <= hi:
            p.append(f"initial_difficulty[{level}] вне границ {lo}..{hi}")
        if not 1 <= lo <= hi <= 3:
            p.append(f"difficulty_range[{level}] вне 1..3")

    # Токены и бюджет (§3.6: резерв отчёта меньше бюджета сессии)
    if tb.report_reserve_input >= tb.session_input_budget:
        p.append("report_reserve_input >= session_input_budget")
    if tb.report_reserve_output >= tb.session_output_budget:
        p.append("report_reserve_output >= session_output_budget")
    # Резерв отчёта покрывает вызовы в пределах общего лимита вызовов на операцию:
    # per-call-limit <= reserve <= max_provider_calls * per-call-limit (§1.3).
    max_calls = lim.max_provider_calls_per_operation
    if not (tb.report_input_max <= tb.report_reserve_input <= max_calls * tb.report_input_max):
        p.append("report_reserve_input вне report_input_max..max_calls*report_input_max")
    if not (tb.report_output_max <= tb.report_reserve_output <= max_calls * tb.report_output_max):
        p.append("report_reserve_output вне report_output_max..max_calls*report_output_max")
    if max(
        tb.assess_input_max + tb.assess_output_max,
        tb.report_input_max + tb.report_output_max,
    ) > tb.model_context_min:
        p.append("операция не помещается в model_context_min")
    if tb.model_context_min < 16000:
        p.append("model_context_min обязан быть не менее 16000 (§1.3)")

    # Heartbeat меньше lease (§3.6)
    if to.worker_heartbeat >= to.worker_lease:
        p.append("worker_heartbeat >= worker_lease")

    # Целевые и аварийные времена (§3.6): вызовы и очередь внутри дедлайнов,
    # SLO p95 существенно ниже аварийных дедлайнов.
    if to.assess_call > to.assessment_deadline_total:
        p.append("assess_call > assessment_deadline_total")
    if to.report_call > to.report_deadline_total:
        p.append("report_call > report_deadline_total")
    if to.stt_call + to.media_download + to.media_decode > to.transcription_deadline_total:
        p.append("stt_call+download+decode > transcription_deadline_total")
    if to.tts_call > to.tts_deadline_total:
        p.append("tts_call > tts_deadline_total")
    if lim.ai_queue_max_seconds + to.assess_call > to.assessment_deadline_total:
        p.append("ai_queue_max_seconds + assess_call > assessment_deadline_total")
    if lim.ai_queue_max_seconds + to.report_call > to.report_deadline_total:
        p.append("ai_queue_max_seconds + report_call > report_deadline_total")
    if slo.next_text_question >= to.assessment_deadline_total:
        p.append("SLO next_text_question >= assessment_deadline_total")
    if slo.text_report >= to.report_deadline_total:
        p.append("SLO text_report >= report_deadline_total")
    if slo.transcription >= to.transcription_deadline_total:
        p.append("SLO transcription >= transcription_deadline_total")
    if slo.voiced_question >= to.tts_deadline_total:
        p.append("SLO voiced_question >= tts_deadline_total")
    if slo.first_text_question > slo.next_text_question:
        p.append("SLO first_text_question > next_text_question")

    # Retention-инварианты (§3.9)
    if ret.session_inactive_expire_hours != 24:
        p.append("session_inactive_expire_hours обязан быть 24 (§1.3)")
    if ret.audio_max_hours > 1:
        p.append("audio_max_hours не более 1 (§3.9)")
    if ret.stt_draft_max_hours > 1:
        p.append("stt_draft_max_hours не более 1 (§3.9)")
    if ret.queue_payload_nullify_hours > 24:
        p.append("queue_payload_nullify_hours не более 24 (§3.9)")
    if ret.deletion_deadline_hours > 24:
        p.append("deletion_deadline_hours не более 24 (§3.9)")
    if ret.completed_content_days != 30:
        p.append("completed_content_days обязан быть 30 (§3.9)")
    if ret.tombstone_days != 7 or ret.backup_rolling_days != 7:
        p.append("tombstone_days и backup_rolling_days обязаны быть 7 (§3.9)")
    if ret.retention_sweep_interval_minutes > 60:
        p.append("retention_sweep_interval_minutes не более 60: очистка каждый час (§3.9)")
    if ret.session_sweep_interval_minutes > 60:
        p.append("session_sweep_interval_minutes не более 60 (§3.9)")
    if s.alerts.worker_heartbeat_missing_seconds <= to.worker_heartbeat:
        p.append("alerts.worker_heartbeat_missing_seconds должен превышать worker_heartbeat")

    # Sender (§1.4/§3.2)
    if send.max_retries > 10:
        p.append("sender.max_retries не более 10 (§3.2)")
    if send.retry_window_hours > 24:
        p.append("sender.retry_window_hours не более 24 (§3.2)")
    if s.inbox.max_handler_retries > 5:
        p.append("inbox.max_handler_retries не более 5 (§3.2)")

    # Перечисления проекта фиксированы (§1.1)
    if [r.value for r in s.roles] != ["python", "analyst", "qa"]:
        p.append("roles обязаны быть python, analyst, qa")
    if [x.value for x in s.levels] != ["intern", "junior"]:
        p.append("levels обязаны быть intern, junior")
    if [x.value for x in s.formats] != ["text", "voice"]:
        p.append("formats обязаны быть text, voice")
    if s.language != "ru":
        p.append("рабочий язык проекта — ru")
    return p


# ---------------------------------------------------------------------------
# questions.json — каталог (§3.3)
# ---------------------------------------------------------------------------


class ScoreGuidance(_Strict):
    s0: str = Field(min_length=1, alias="0")
    s1: str = Field(min_length=1, alias="1")
    s2: str = Field(min_length=1, alias="2")
    s3: str = Field(min_length=1, alias="3")
    s4: str = Field(min_length=1, alias="4")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Rubric(_Strict):
    key_points: list[str] = Field(min_length=1)
    evidence_criteria: list[str] | None = None
    score_guidance: ScoreGuidance


class CatalogEntry(_Strict):
    id: str = Field(min_length=1)
    question_type: QuestionType
    main_index: int = Field(ge=1, le=6)
    topic: str = Field(min_length=1)
    question: str | None = None
    difficulty_variants: dict[str, str] | None = None
    rubric: Rubric
    fallback_question: str = Field(min_length=1)
    advice: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> CatalogEntry:
        if self.question_type is QuestionType.hr:
            if not self.question:
                raise ValueError(f"{self.id}: HR-запись обязана иметь question")
            if self.difficulty_variants is not None:
                raise ValueError(f"{self.id}: HR-запись не имеет difficulty_variants")
            if not self.rubric.evidence_criteria:
                raise ValueError(f"{self.id}: HR-рубрика обязана иметь evidence_criteria")
        else:
            if self.question is not None:
                raise ValueError(f"{self.id}: technical-запись использует difficulty_variants")
            if not self.difficulty_variants:
                raise ValueError(f"{self.id}: technical-запись обязана иметь difficulty_variants")
            if self.rubric.evidence_criteria is not None:
                raise ValueError(f"{self.id}: evidence_criteria только для HR")
        return self

    def question_for_difficulty(self, difficulty: int) -> str:
        """Вопрос для текущей сложности: вариант каталога либо запасной вопрос."""
        if self.question_type is QuestionType.hr:
            return self.question or self.fallback_question
        variants = self.difficulty_variants or {}
        key = str(difficulty)
        if key in variants:
            return variants[key]
        # Ближайший доступный вариант (границы уровня), иначе запасной вопрос.
        if variants:
            nearest = min(variants, key=lambda k: abs(int(k) - difficulty))
            return variants[nearest]
        return self.fallback_question


class CatalogScenario(_Strict):
    role_id: RoleId
    level: Level
    entries: list[CatalogEntry]


class Catalog(_Strict):
    version: str
    note: str | None = None
    hr_topics: list[str]
    technical_topics: dict[RoleId, list[str]]
    scenarios: list[CatalogScenario]


def validate_catalog(catalog: Catalog, settings: Settings) -> list[str]:
    """Структурная валидность каталога (§3.3): 6 сценариев, 36 записей, темы."""
    p: list[str] = []
    if catalog.version != CONFIG_VERSION:
        p.append(f"questions.version={catalog.version}, ожидается {CONFIG_VERSION}")
    if catalog.hr_topics != HR_TOPICS_ORDERED:
        p.append(f"hr_topics обязаны быть {HR_TOPICS_ORDERED}")
    for role in RoleId:
        topics = catalog.technical_topics.get(role)
        if not topics or len(topics) != settings.interview.technical_questions:
            p.append(f"technical_topics[{role.value}] обязаны содержать "
                     f"{settings.interview.technical_questions} темы")

    expected_pairs = {(r, lv) for r in RoleId for lv in Level}
    seen_pairs: set[tuple[RoleId, Level]] = set()
    all_ids: set[str] = set()
    for scenario in catalog.scenarios:
        key = (scenario.role_id, scenario.level)
        if key in seen_pairs:
            p.append(f"дублирующийся сценарий {key}")
        seen_pairs.add(key)
        entries = scenario.entries
        if len(entries) != settings.interview.main_questions:
            p.append(f"{key}: {len(entries)} записей вместо "
                     f"{settings.interview.main_questions}")
            continue
        expected_topics = (
            HR_TOPICS_ORDERED + catalog.technical_topics.get(scenario.role_id, [])
        )
        for position, entry in enumerate(entries):
            prefix = f"{key} #{position + 1}"
            if entry.id in all_ids:
                p.append(f"{prefix}: дублирующийся id {entry.id}")
            all_ids.add(entry.id)
            if entry.main_index != position + 1:
                p.append(f"{prefix}: main_index={entry.main_index}, ожидается {position + 1}")
            if entry.topic != expected_topics[position]:
                p.append(
                    f"{prefix}: topic={entry.topic}, ожидается {expected_topics[position]}"
                )
            expected_type = (
                QuestionType.hr
                if position < settings.interview.hr_questions
                else QuestionType.technical
            )
            if entry.question_type is not expected_type:
                p.append(f"{prefix}: question_type={entry.question_type.value}, "
                         f"ожидается {expected_type.value}")
            # длины всех формулировок
            texts: list[str] = [entry.fallback_question]
            if entry.question:
                texts.append(entry.question)
            texts.extend((entry.difficulty_variants or {}).values())
            for text in texts:
                if not 1 <= len(text) <= settings.limits.question_max_chars:
                    p.append(f"{prefix}: длина формулировки вне 1.."
                             f"{settings.limits.question_max_chars}")
                    break
            # варианты сложности покрывают диапазон уровня
            if entry.question_type is QuestionType.technical:
                lo = settings.interview.difficulty_min[scenario.level]
                hi = settings.interview.difficulty_max[scenario.level]
                required = {str(d) for d in range(lo, hi + 1)}
                actual = set((entry.difficulty_variants or {}).keys())
                if not required.issubset(actual):
                    p.append(f"{prefix}: difficulty_variants {sorted(actual)} "
                             f"не покрывает диапазон уровня {sorted(required)}")
                for d in actual - {str(x) for x in range(1, 4)}:
                    p.append(f"{prefix}: недопустимая сложность {d}")
            # рубрика
            if any(not kp.strip() for kp in entry.rubric.key_points):
                p.append(f"{prefix}: пустой key_points в рубрике")

    missing = expected_pairs - seen_pairs
    if missing:
        p.append(f"отсутствуют сценарии: {sorted(str(m) for m in missing)}")
    total = sum(len(sc.entries) for sc in catalog.scenarios)
    if total != 36:
        p.append(f"каталог содержит {total} записей вместо 36")
    return p


# ---------------------------------------------------------------------------
# products.json (§3.8) и provider-profile (§1.4)
# ---------------------------------------------------------------------------


class Product(_Strict):
    product_id: str
    title_ru: str
    units: int = Field(gt=0)
    price_rub_reference: float | None = Field(default=None, gt=0)
    price_xtr: int | None = Field(default=None, gt=0)


class Products(_Strict):
    version: str
    billing_enabled: bool
    currency: str
    note: str | None = None
    products: list[Product]
    refund_policy: dict[str, Any] | None = None


def validate_products(products: Products) -> list[str]:
    p: list[str] = []
    if products.version != CONFIG_VERSION:
        p.append(f"products.version={products.version}, ожидается {CONFIG_VERSION}")
    if products.currency != "XTR":
        p.append("цифровая услуга внутри бота оплачивается currency=XTR")
    expected = {"single": 1, "pack5": 5, "pack10": 10}
    actual = {pr.product_id: pr.units for pr in products.products}
    if actual != expected:
        p.append(f"products обязаны быть single/pack5/pack10 c units 1/5/10, получено {actual}")
    if products.billing_enabled:
        for pr in products.products:
            if pr.price_xtr is None:
                p.append(
                    f"billing_enabled=true, но {pr.product_id} не имеет положительной цены XTR"
                )
    return p


class ProviderSmoke(_Strict):
    model_config = ConfigDict(extra="allow")


class ProviderBlock(_Strict):
    """Блок провайдера: null = незаполненная внешняя зависимость (§1.4)."""

    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    region: str | None = None
    data_retention_terms: str | None = None


class LlmProviderBlock(ProviderBlock):
    context_window_tokens: int | None = Field(default=None, ge=1)
    temperature_supported: bool | None = None
    native_json_schema_supported: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)


class ProviderProfile(BaseModel):
    """Профиль провайдеров: extra=allow допускает комментарии и эволюцию
    формата; null-значения означают незаполненную внешнюю зависимость (§1.4)."""

    model_config = ConfigDict(extra="allow")

    version: str
    llm: LlmProviderBlock
    stt: ProviderBlock
    tts: ProviderBlock
    rules: dict[str, Any] | None = None

    def is_complete(self, env: dict[str, str]) -> tuple[bool, list[str]]:
        """Профиль заполнен, если нет null-значений, присутствуют ключи
        окружения и выполнены smoke-проверки (T01, §1.4/§5.3).

        Незаполненный профиль блокирует AI-вызовы, но не webhook (§1.4).
        """
        missing: list[str] = []
        if self.version != CONFIG_VERSION:
            missing.append("version")
        for name in ("llm", "stt", "tts"):
            block = getattr(self, name)
            for fname in ("provider", "model", "endpoint"):
                if getattr(block, fname, None) in (None, ""):
                    missing.append(f"{name}.{fname}")
            key_env = block.api_key_env
            if not key_env:
                missing.append(f"{name}.api_key_env")
            elif not env.get(key_env):
                missing.append(f"env:{key_env}")
            if block.region in (None, ""):
                missing.append(f"{name}.region")
            if block.data_retention_terms in (None, ""):
                missing.append(f"{name}.data_retention_terms")
            smoke = getattr(block, "smoke", None)
            if not isinstance(smoke, dict) or not smoke or any(
                v in (None, "", False) for v in smoke.values()
            ):
                missing.append(f"{name}.smoke")
        if self.llm.context_window_tokens is None:
            missing.append("llm.context_window_tokens")
        elif self.llm.context_window_tokens < 16000:
            missing.append("llm.context_window_tokens<16000")
        return (not missing), missing


# ---------------------------------------------------------------------------
# Переменные окружения и секреты (§3.9: секреты в окружении, не в коде/логах)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvConfig:
    bot_token: str
    webhook_secret: str
    webhook_route_token: str
    database_url: str
    migrations_database_url: str
    settings_path: Path
    questions_path: Path
    products_path: Path
    provider_profile_path: Path
    prompts_dir: Path
    media_dir: Path
    run_dir: Path
    api_host: str
    api_port: int
    env_name: str
    log_level: str
    support_contact: str | None
    public_base_url: str | None

    @classmethod
    def from_environ(cls, env: dict[str, str] | None = None) -> EnvConfig:
        e = dict(os.environ if env is None else env)

        def required(name: str) -> str:
            value = (e.get(name) or "").strip()
            if not value:
                raise ConfigError([f"обязательная переменная окружения {name} не задана"])
            return value

        bot_token = required("BOT_TOKEN")
        webhook_secret = required("WEBHOOK_SECRET")
        route_token = required("WEBHOOK_ROUTE_TOKEN")
        database_url = required("DATABASE_URL")

        problems: list[str] = []
        for name, value in (
            ("WEBHOOK_SECRET", webhook_secret),
            ("WEBHOOK_ROUTE_TOKEN", route_token),
        ):
            if not value.isascii():
                problems.append(f"{name} обязан быть ASCII")
            if not 16 <= len(value) <= 255:
                problems.append(f"{name} обязан быть 16..255 символов")
        # route_token случайный и не содержит токен бота (§3.1)
        if route_token in bot_token or bot_token in route_token:
            problems.append("WEBHOOK_ROUTE_TOKEN не должен содержать токен бота")
        env_name = e.get("ENV_NAME", "dev").strip() or "dev"
        if env_name not in ("dev", "staging", "production"):
            problems.append("ENV_NAME обязан быть dev|staging|production")
        try:
            api_port = int(e.get("API_PORT", "8080"))
            if not 1 <= api_port <= 65535:
                raise ValueError
        except ValueError:
            problems.append("API_PORT обязан быть целым 1..65535")
            api_port = 8080
        if problems:
            raise ConfigError(problems)

        return cls(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            webhook_route_token=route_token,
            database_url=database_url,
            migrations_database_url=(
                e.get("MIGRATIONS_DATABASE_URL", "").strip() or database_url
            ),
            settings_path=Path(e.get("SETTINGS_PATH", "config/settings.json")),
            questions_path=Path(e.get("QUESTIONS_PATH", "config/questions.json")),
            products_path=Path(e.get("PRODUCTS_PATH", "config/products.json")),
            provider_profile_path=Path(
                e.get("PROVIDER_PROFILE_PATH", "config/provider-profile.json")
            ),
            prompts_dir=Path(e.get("PROMPTS_DIR", "prompts")),
            media_dir=Path(e.get("MEDIA_DIR", "/var/lib/prosobesedovanie/media")),
            run_dir=Path(e.get("RUN_DIR", "/var/lib/prosobesedovanie/run")),
            api_host=e.get("API_HOST", "0.0.0.0"),
            api_port=api_port,
            env_name=env_name,
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
            support_contact=e.get("SUPPORT_CONTACT", "").strip() or None,
            public_base_url=e.get("PUBLIC_BASE_URL", "").strip() or None,
        )


# ---------------------------------------------------------------------------
# Промпты (§3.6: отдельные неизменяемые файлы, версия 2.0)
# ---------------------------------------------------------------------------

PROMPT_FILES = ("turn-system.txt", "report-system.txt", "user-envelope.txt")


def load_prompts(prompts_dir: Path) -> dict[str, str]:
    problems: list[str] = []
    prompts: dict[str, str] = {}
    for name in PROMPT_FILES:
        path = prompts_dir / name
        if not path.is_file():
            problems.append(f"промпт {name} отсутствует")
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            problems.append(f"промпт {name} пуст")
        version_marks = (f"ВЕРСИЯ: {CONFIG_VERSION}", f"ВЕРСИЯ ПРОМПТА: {CONFIG_VERSION}")
        if not any(mark in text for mark in version_marks):
            problems.append(f"промпт {name} не содержит отметку версии {CONFIG_VERSION}")
        prompts[name] = text
    if problems:
        raise ConfigError(problems)
    return prompts


# ---------------------------------------------------------------------------
# Сборка конфигурации приложения
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    env: EnvConfig
    settings: Settings
    catalog: Catalog
    products: Products
    prompts: dict[str, str]
    provider_profile: ProviderProfile | None
    warnings: list[str] = field(default_factory=list)

    @property
    def ai_enabled(self) -> bool:
        return self._ai_ready

    _ai_ready: bool = field(default=False, init=False, repr=False)
    ai_missing: list[str] = field(default_factory=list, init=False)

    def finalize_ai_state(self, env: dict[str, str]) -> None:
        complete = False
        missing: list[str] = []
        if self.provider_profile is None:
            missing.append("provider-profile.json отсутствует (скопируйте example и заполните)")
        else:
            complete, missing = self.provider_profile.is_complete(env)
        self._ai_ready = complete
        self.ai_missing = missing
        if not complete:
            self.warnings.append(
                "AI-вызовы заблокированы: незаполненные внешние зависимости: "
                + ", ".join(missing)
            )
        if not self.settings.flags.voice_enabled:
            self.warnings.append(
                "voice_enabled=false: сборка является промежуточным прототипом, "
                "не функциональным MVP (§3.6)"
            )
        if self.settings.costs.daily_ai_budget_usd is None:
            self.warnings.append(
                "daily_ai_budget_usd не заполнен: до включения внешних вызовов "
                "внести тарифы и бюджет (§4.3)"
            )


def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError([f"{what}: файл {path} не найден"]) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{what}: {path} не является валидным JSON: {exc}"]) from exc


def load_config(env: dict[str, str] | None = None) -> AppConfig:
    """Загрузка и полная startup-валидация. Любая проблема -> ConfigError."""
    env_config = EnvConfig.from_environ(env)
    environ = dict(os.environ if env is None else env)

    settings_raw = _load_json(env_config.settings_path, "settings")
    try:
        settings = Settings.model_validate(settings_raw)
    except ValidationError as exc:
        raise ConfigError([f"settings: {e} " for e in exc.errors()[:5]]) from exc

    catalog_raw = _load_json(env_config.questions_path, "questions")
    try:
        catalog = Catalog.model_validate(catalog_raw)
    except ValidationError as exc:
        raise ConfigError([f"questions: {e} " for e in exc.errors()[:5]]) from exc

    products_raw = _load_json(env_config.products_path, "products")
    try:
        products = Products.model_validate(products_raw)
    except ValidationError as exc:
        raise ConfigError([f"products: {e} " for e in exc.errors()[:5]]) from exc

    problems: list[str] = []
    problems.extend(validate_settings_consistency(settings))
    problems.extend(validate_catalog(catalog, settings))
    problems.extend(validate_products(products))
    if problems:
        raise ConfigError(problems)

    prompts = load_prompts(env_config.prompts_dir)

    provider_profile: ProviderProfile | None = None
    profile_path = env_config.provider_profile_path
    if profile_path.is_file():
        profile_raw = _load_json(profile_path, "provider-profile")
        try:
            provider_profile = ProviderProfile.model_validate(profile_raw)
        except ValidationError as exc:
            raise ConfigError([f"provider-profile: {e}" for e in exc.errors()[:5]]) from exc

    config = AppConfig(
        env=env_config,
        settings=settings,
        catalog=catalog,
        products=products,
        prompts=prompts,
        provider_profile=provider_profile,
    )
    config.finalize_ai_state(environ)
    for warning in config.warnings:
        logger.warning("config: %s", warning)
    return config
