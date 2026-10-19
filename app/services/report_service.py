"""Персональный отчёт (§2.2, §3.4; задачи T13, T14).

Полный план или явное «Завершить» -> снимок фактических вопросов, ответов,
пропусков и оценок -> report-задание -> модель возвращает FeedbackReport 2.0
-> проверка схемы и доказательств -> сохранение reports и всех обязательных
частей outbox в одной транзакции -> последовательная доставка -> после
подтверждения всех частей completed и перевод резерва попытки в consumed.

Если ответы целиком не помещаются в токен-лимит, они НЕ обрезаются без
обозначения: используется детерминированный отчёт по сохранённым оценкам и
фактическим цитатам, помеченный degraded. Детерминированный отчёт не создаёт
фиктивных цитат или оценок.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import timedelta
from typing import Any

import asyncpg

from app.config import AppConfig
from app.providers.llm import (
    LlmConfigError,
    LlmFatalError,
    LlmRequest,
    LlmResult,
    LlmRetryableError,
    LlmUnknownResultError,
)
from app.services import metrics, session_service, token_budget
from app.services.token_budget import estimate_tokens
from app.services.ui_tokens import generate_ui_token
from app.storage import (
    credits_repo,
    events_repo,
    jobs_repo,
    outbox_repo,
    sessions_repo,
    turns_repo,
    ui_tokens_repo,
)
from app.telegram import messages
from app.telegram.markup import buttons_payload, text_payload
from contracts.models import (
    MESSAGE_PART_MAX_CHARS,
    REPORT_MAX_CHARS,
    Coverage,
    FeedbackReport,
    GenerateReportInput,
    QuestionType,
    ReportAction,
    ReportFinding,
    SessionStatus,
    SnapshotTurn,
    TurnAssessment,
    TurnKind,
)
from contracts.semantic_validation import quote_matches, validate_report

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Снимок сессии (§3.4)
# ---------------------------------------------------------------------------


def build_snapshot(turns: list[asyncpg.Record]) -> list[SnapshotTurn]:
    snapshot: list[SnapshotTurn] = []
    for row in turns:
        assessment: TurnAssessment | None = None
        raw = row["assessment"]
        if raw is not None:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                assessment = TurnAssessment.model_validate(data)
            except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                assessment = None  # невалидная сохранённая оценка -> NULL
        snapshot.append(
            SnapshotTurn(
                turn_id=row["id"],
                ordinal=row["ordinal"],
                kind=TurnKind(row["kind"]),
                question_type=QuestionType(row["question_type"]),
                main_index=row["main_index"],
                topic=row["topic"],
                question=row["question_text"],
                answer_text=row["answer_text"],
                # защита: turn без подтверждённого ответа трактуется снимком
                # как пропущенный (§3.3) — отчёт не может падать на таком
                skipped=bool(row["skipped"]) or row["answer_text"] is None,
                assessment=assessment if row["answer_text"] is not None else None,
            )
        )
    return snapshot


def compute_partial(snapshot: list[SnapshotTurn], main_total: int) -> bool:
    """partial=true при завершении до прохождения всех основных тем (§3.4)."""
    seen_main = {t.main_index for t in snapshot if t.kind is TurnKind.main}
    return len(seen_main) < main_total


# ---------------------------------------------------------------------------
# Детерминированный запасной отчёт (§3.4)
# ---------------------------------------------------------------------------


def build_deterministic_report(
    config: AppConfig,
    *,
    session: asyncpg.Record,
    snapshot: list[SnapshotTurn],
    partial: bool,
) -> FeedbackReport:
    """Строится без выдуманных выводов: темы и пропуски, валидные сохранённые
    оценки и найденные цитаты; missing_points связываются с рубрикой и
    советами каталога; без фиктивных цитат и оценок; degraded=true."""
    strengths: list[ReportFinding] = []
    gaps: list[ReportFinding] = []
    actions: list[ReportAction] = []

    scenario = session_service.scenario_for(config, session["role_id"], session["level"])
    entries_by_topic = {e.topic: e for e in scenario.entries}

    for turn in snapshot:
        entry = entries_by_topic.get(turn.topic)
        if turn.skipped:
            gaps.append(
                ReportFinding(
                    turn_id=turn.turn_id,
                    quote=None,
                    skipped=True,
                    comment=f"Тема «{turn.topic}» пропущена — ответ не оценивался.",
                )
            )
            continue
        assessment = turn.assessment
        if assessment is None:
            continue  # упоминается в разделе тем как «оценка недоступна»
        topic_score = (
            assessment.scores.correctness
            if turn.question_type is QuestionType.technical
            else assessment.scores.evidence
        )
        valid_quotes = [
            q
            for q in assessment.evidence_quotes
            if turn.answer_text and quote_matches(q, turn.answer_text)
        ]
        if topic_score is not None and topic_score >= 3 and valid_quotes and len(strengths) < 5:
            strengths.append(
                ReportFinding(
                    turn_id=turn.turn_id,
                    quote=valid_quotes[0],
                    skipped=False,
                    comment=(
                        f"Тема «{turn.topic}»: ответ раскрыт на {topic_score} из 4 "
                        f"по сохранённой оценке."
                    ),
                )
            )
        if assessment.missing_points and valid_quotes and len(gaps) < 5:
            gaps.append(
                ReportFinding(
                    turn_id=turn.turn_id,
                    quote=valid_quotes[0],
                    skipped=False,
                    comment=(
                        f"Тема «{turn.topic}»: не раскрыто — "
                        + "; ".join(assessment.missing_points[:4])
                        + "."
                    ),
                )
            )

    # Приоритетные действия: из пробелов (советы каталога), иначе закрепление
    turns_by_id = {t.turn_id: t for t in snapshot}
    for gap in gaps[:3]:
        turn = turns_by_id.get(gap.turn_id)
        entry = entries_by_topic.get(turn.topic) if turn else None
        advice = entry.advice if entry else "Повторите материал темы и потренируйте ответ вслух."
        actions.append(
            ReportAction(
                priority=len(actions) + 1,
                related_turn_id=gap.turn_id,
                text=f"Тема «{turn.topic if turn else ''}»: {advice}",
            )
        )
    if not actions:
        anchor = next((t for t in reversed(snapshot) if not t.skipped), snapshot[0])
        actions.append(
            ReportAction(
                priority=1,
                related_turn_id=anchor.turn_id,
                text=(
                    "Закрепите результат: повторите ответы по разобранным темам "
                    "вслух и пройдите новую тренировку через несколько дней."
                ),
            )
        )

    main_turns = [t for t in snapshot if t.kind is TurnKind.main]
    return FeedbackReport(
        session_id=session["id"],
        partial=partial,
        coverage=Coverage(
            main_total=len(main_turns),
            main_answered=sum(1 for t in main_turns if not t.skipped),
            main_skipped=sum(1 for t in main_turns if t.skipped),
            follow_ups=sum(1 for t in snapshot if t.kind is TurnKind.follow_up),
        ),
        strengths=strengths,
        gaps=gaps,
        actions=actions[:3],
        improved_examples=[],  # пример не выдумывает факты — в запасном отчёте их нет
    )


# ---------------------------------------------------------------------------
# Форматирование в plain text и разбиение на части (§3.4)
# ---------------------------------------------------------------------------


def format_report_text(
    config: AppConfig,
    report: FeedbackReport,
    *,
    session: asyncpg.Record,
    snapshot: list[SnapshotTurn],
    degraded: bool,
) -> str:
    """Порядок: параметры и полнота -> разобранные темы -> сильные стороны ->
    ошибки с цитатами и исправлениями -> приоритетные действия -> улучшенные
    ответы (§3.4). Plain text, без исполнения Markdown/HTML модели."""
    from contracts.models import InterviewFormat, Level, RoleId

    role_label = messages.ROLE_LABELS[RoleId(session["role_id"]).value]
    level_label = messages.LEVEL_LABELS[Level(session["level"]).value]
    fmt_label = messages.FORMAT_LABELS[InterviewFormat(session["format"]).value]

    blocks: list[str] = [messages.REPORT_TITLE]
    labels = []
    if report.partial:
        labels.append(messages.REPORT_PARTIAL_LABEL)
    if degraded:
        labels.append(messages.REPORT_DEGRADED_LABEL)
    if labels:
        blocks.append(" · ".join(labels))
    blocks.append(
        messages.REPORT_PARAMS.format(role=role_label, level=level_label, fmt=fmt_label)
    )
    cov = report.coverage
    blocks.append(
        messages.REPORT_COVERAGE.format(
            answered=cov.main_answered,
            skipped=cov.main_skipped,
            total=cov.main_total,
            follow_ups=cov.follow_ups,
        )
    )
    if degraded:
        blocks.append(
            "Разбор подготовлен по сохранённым оценкам и фактическим цитатам "
            "без новых выводов модели."
        )

    # Разобранные темы
    entries = {
        e.topic: e
        for e in session_service.scenario_for(
            config, session["role_id"], session["level"]
        ).entries
    }
    topic_lines = [messages.REPORT_TOPICS_TITLE]
    for turn in snapshot:
        if turn.kind is TurnKind.follow_up:
            continue
        entry = entries.get(turn.topic)
        label = _TOPIC_RU.get(turn.topic, turn.topic)
        index = turn.main_index or 0
        if turn.skipped:
            topic_lines.append(f"{index}. {label} — пропущено")
            continue
        if turn.assessment is None:
            advice = entry.advice if entry else "повторите материал темы"
            topic_lines.append(f"{index}. {label} — отвечено, оценка недоступна")
            topic_lines.append(
                "   " + messages.REPORT_NO_ASSESSMENT.format(advice=advice)
            )
        else:
            topic_lines.append(f"{index}. {label} — отвечено и оценено")
    blocks.append("\n".join(topic_lines))

    # Сильные стороны
    strength_lines = [messages.REPORT_STRENGTHS_TITLE]
    if report.strengths:
        for finding in report.strengths:
            turn = _turn_of(snapshot, finding.turn_id)
            label = _TOPIC_RU.get(turn.topic, turn.topic) if turn else "?"
            strength_lines.append(f"• {label}: {finding.comment}")
            if finding.quote:
                strength_lines.append(
                    "  " + messages.REPORT_QUOTE_PREFIX.format(quote=finding.quote)
                )
    else:
        strength_lines.append(messages.REPORT_NO_STRENGTHS)
    blocks.append("\n".join(strength_lines))

    # Ошибки и пробелы
    gap_lines = [messages.REPORT_GAPS_TITLE]
    turns_by_id = {t.turn_id: t for t in snapshot}
    if report.gaps:
        for finding in report.gaps:
            turn = turns_by_id.get(finding.turn_id)
            label = _TOPIC_RU.get(turn.topic, turn.topic) if turn else "?"
            if finding.skipped:
                gap_lines.append(f"• {label}: пропуск темы.")
                gap_lines.append("  " + messages.REPORT_SKIPPED_NOTE)
                continue
            gap_lines.append(f"• {label}: {finding.comment}")
            if finding.quote:
                gap_lines.append(
                    "  " + messages.REPORT_QUOTE_PREFIX.format(quote=finding.quote)
                )
    else:
        gap_lines.append("Существенных пробелов по сохранённым оценкам не выявлено.")
    blocks.append("\n".join(gap_lines))

    # Приоритетные действия
    action_lines = [messages.REPORT_ACTIONS_TITLE]
    for action in sorted(report.actions, key=lambda a: a.priority):
        action_lines.append(f"{action.priority}. {action.text}")
    blocks.append("\n".join(action_lines))

    # Улучшенные примеры
    if report.improved_examples:
        example_lines = [messages.REPORT_EXAMPLES_TITLE]
        for example in report.improved_examples:
            turn = turns_by_id.get(example.turn_id)
            label = _TOPIC_RU.get(turn.topic, turn.topic) if turn else "?"
            example_lines.append(f"• {label}:")
            example_lines.append(example.text)
            if example.uses_placeholders:
                example_lines.append(
                    "  (в квадратных скобках — места для ваших фактов)"
                )
        blocks.append("\n".join(example_lines))

    return "\n\n".join(blocks)


_TOPIC_RU = {
    "self_introduction": "Самопрезентация",
    "project_story": "Учебный/личный проект",
    "collections": "Коллекции Python",
    "exceptions": "Исключения",
    "scoping": "Области видимости",
    "data_processing": "Поиск и обработка данных",
    "sql_filtering": "SQL-фильтрация",
    "sql_joins": "SQL JOIN",
    "data_quality": "Качество данных",
    "metrics": "Метрики",
    "test_design": "Тест-дизайн",
    "bug_report": "Баг-репорт",
    "prioritization": "Приоритизация",
    "api_testing": "Тестирование API",
}


def _turn_of(snapshot: list[SnapshotTurn], turn_id: uuid.UUID) -> SnapshotTurn | None:
    for turn in snapshot:
        if turn.turn_id == turn_id:
            return turn
    return None


def split_into_parts(
    text: str,
    *,
    limit: int = MESSAGE_PART_MAX_CHARS,
    max_parts: int = 4,
) -> list[str]:
    """1–4 части по ≤3500 символов; разбиение по смысловым блокам (пустая
    строка), при необходимости — по строкам; цитата не разрывается, если
    помещается в часть (§1.3, §3.4)."""
    parts = _pack_units(text.split("\n\n"), "\n\n", limit)
    if len(parts) <= max_parts:
        return parts
    # плотнее: деление по отдельным строкам
    return _pack_units(text.split("\n"), "\n", limit)


def _pack_units(units: list[str], joiner: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > limit:
            pieces = _split_long_block(unit, limit)
        else:
            pieces = [unit]
        for piece in pieces:
            if not piece and not current:
                continue
            if current and len(current) + len(joiner) + len(piece) > limit:
                parts.append(current)
                current = piece
            else:
                current = f"{current}{joiner}{piece}" if current else piece
    if current:
        parts.append(current)
    return parts or [""]


def _split_long_block(block: str, limit: int) -> list[str]:
    """Деление длинного блока по границам предложений вне кавычек;
    цитата длиннее предела делится вынужденно (физически не помещается)."""
    pieces: list[str] = []
    for line in block.split("\n"):
        while len(line) > limit:
            cut = _safe_cut(line, limit)
            pieces.append(line[:cut])
            line = line[cut:].lstrip()
        pieces.append(line)
    return pieces


def _safe_cut(line: str, limit: int) -> int:
    """Точка разреза ≤ limit: не внутри цитаты, если цитата помещается в
    часть; предпочтительно граница предложения."""
    window = line[:limit]
    if window.count("«") > window.count("»"):
        open_pos = window.rfind("«")
        if open_pos > 0:
            return open_pos  # цитата целиком уходит в следующую часть
        # цитата начинается с окна и длиннее предела: вынужденное деление
        for boundary in (". ", "! ", "? ", "; ", ", ", " "):
            pos = window.rfind(boundary)
            if pos > limit // 2:
                return pos + 1
        return limit
    for boundary in (". ", "! ", "? ", "; ", ", "):
        pos = window.rfind(boundary)
        if pos > limit // 2:
            return pos + 1
    return limit


def shrink_to_limit(
    config: AppConfig,
    report: FeedbackReport,
    *,
    session: asyncpg.Record,
    snapshot: list[SnapshotTurn],
    degraded: bool,
) -> tuple[str, list[str]]:
    """Формальный максимум полей может дать длинный отчёт: сокращаем
    второстепенные пояснения и количество примеров, сохраняя минимум одно
    действие и связь ошибки с фактическим ответом (§3.4). Гарантирует
    ≤12000 символов и ≤4 частей по ≤3500."""

    def render(rep: FeedbackReport) -> str:
        return format_report_text(
            config, rep, session=session, snapshot=snapshot, degraded=degraded
        )

    def _trim(findings: list[ReportFinding], cap: int) -> list[ReportFinding]:
        return [
            f.model_copy(update={"comment": f.comment[:cap]})
            if len(f.comment) > cap
            else f
            for f in findings
        ]

    text = render(report)

    # 1) сокращаем число примеров, затем убираем примеры полностью
    if len(text) > REPORT_MAX_CHARS and len(report.improved_examples) > 1:
        report = report.model_copy(
            update={"improved_examples": report.improved_examples[:1]}
        )
        text = render(report)
    if len(text) > REPORT_MAX_CHARS and report.improved_examples:
        report = report.model_copy(update={"improved_examples": []})
        text = render(report)

    # 2) сокращаем длинные комментарии наблюдений (цитаты и действия не трогаем)
    for cap in (400, 200, 120):
        if len(text) <= REPORT_MAX_CHARS:
            break
        report = report.model_copy(
            update={
                "strengths": _trim(report.strengths, cap),
                "gaps": _trim(report.gaps, cap),
            }
        )
        text = render(report)

    # 3) сокращаем число пунктов, пока не выполнится лимит частей
    for keep in (5, 3, 2, 1):
        parts = split_into_parts(text)
        if len(text) <= REPORT_MAX_CHARS and len(parts) <= 4:
            return text, parts
        report = report.model_copy(
            update={
                "strengths": report.strengths[:keep],
                "gaps": report.gaps[:keep],
            }
        )
        text = render(report)

    # 4) крайний случай: жёсткая обрезка по пределу на границе абзаца
    parts = split_into_parts(text)
    if len(text) > REPORT_MAX_CHARS:
        cut = text[:REPORT_MAX_CHARS]
        last_break = cut.rfind("\n\n")
        if last_break > REPORT_MAX_CHARS // 2:
            cut = cut[:last_break]
        text = cut
        parts = split_into_parts(text)
    if len(parts) > 4:
        parts = parts[:4]
    return text, parts


# ---------------------------------------------------------------------------
# Обработка report-задания (§2.2, §3.4)
# ---------------------------------------------------------------------------


async def process_report_job(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    owner: str,
    *,
    force_deterministic: bool = False,
) -> None:
    config: AppConfig = deps.config
    settings = config.settings

    async with pool.acquire() as conn:
        session = await sessions_repo.find_by_id(conn, job["session_id"])
        turns = await turns_repo.list_by_session(conn, job["session_id"]) if session else []
    if session is None or not turns:
        await _finish_job(pool, job, owner, "failed", error_code="CONTEXT_GONE")
        return

    snapshot = build_snapshot(turns)
    partial = compute_partial(snapshot, settings.interview.main_questions)

    report: FeedbackReport
    degraded = False
    system_prompt = config.prompts["report-system.txt"]
    input_data = GenerateReportInput(
        session_id=session["id"],
        role_id=session["role_id"],
        level=session["level"],
        partial=partial,
        snapshot_turns=snapshot,
    )
    user_payload = json.dumps(
        input_data.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    est_input = estimate_tokens(system_prompt + user_payload)

    if force_deterministic or deps.llm is None or not config.ai_enabled:
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=partial
        )
        degraded = True
        metrics.REGISTRY.inc(metrics.FALLBACK, operation="report", reason="ai_disabled")
    elif est_input > settings.tokens_budget.report_input_max:
        # Ответы целиком не помещаются: не обрезать без обозначения (§3.4)
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=partial
        )
        degraded = True
        metrics.REGISTRY.inc(metrics.FALLBACK, operation="report", reason="input_overflow")
    else:
        validated = await _call_llm_report(
            pool, deps, job, system_prompt, user_payload, est_input,
            snapshot=snapshot, session_id=session["id"], partial=partial,
        )
        if validated is None:
            report = build_deterministic_report(
                config, session=session, snapshot=snapshot, partial=partial
            )
            degraded = True
            metrics.REGISTRY.inc(metrics.FALLBACK, operation="report", reason="llm_unavailable")
        else:
            report = validated

    # Самопроверка (детерминированный отчёт обязан проходить валидацию)
    final_issues = validate_report(
        report,
        snapshot=snapshot,
        expected_session_id=session["id"],
        expected_partial=partial,
    )
    if final_issues:
        logger.error("report self-validation failed", extra={"n": len(final_issues)})
        report = _minimal_report(session=session, snapshot=snapshot, partial=partial)
        degraded = True
        metrics.REGISTRY.inc(metrics.FALLBACK, operation="report", reason="self_validation")

    text, parts = shrink_to_limit(
        config, report, session=session, snapshot=snapshot, degraded=degraded
    )
    await _save_and_enqueue(pool, deps, job, owner, session=session, report=report,
                            parts=parts, partial=partial, degraded=degraded,
                            total_chars=len(text))


def _minimal_report(
    *, session: asyncpg.Record, snapshot: list[SnapshotTurn], partial: bool
) -> FeedbackReport:
    """Крайний запасной вариант: только факты покрытия и одно действие."""
    main_turns = [t for t in snapshot if t.kind is TurnKind.main]
    anchor = next((t for t in reversed(snapshot) if not t.skipped), snapshot[0])
    return FeedbackReport(
        session_id=session["id"],
        partial=partial,
        coverage=Coverage(
            main_total=len(main_turns),
            main_answered=sum(1 for t in main_turns if not t.skipped),
            main_skipped=sum(1 for t in main_turns if t.skipped),
            follow_ups=sum(1 for t in snapshot if t.kind is TurnKind.follow_up),
        ),
        strengths=[],
        gaps=[
            ReportFinding(turn_id=t.turn_id, quote=None, skipped=True,
                          comment="Тема пропущена.")
            for t in main_turns if t.skipped
        ][:5],
        actions=[
            ReportAction(
                priority=1,
                related_turn_id=anchor.turn_id,
                text="Пройдите новую тренировку, чтобы разобрать пропущенные темы.",
            )
        ],
        improved_examples=[],
    )


async def _call_llm_report(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    system_prompt: str,
    user_payload: str,
    est_input: int,
    *,
    snapshot: list[SnapshotTurn],
    session_id: uuid.UUID,
    partial: bool,
) -> FeedbackReport | None:
    """≤2 вызова (один повтор допустим). None — запасной отчёт.

    Бюджет: отчёт расходует сохранённый для него резерв — проверка идёт
    против полного бюджета сессии (включая резерв), а не против доступного
    для оценок остатка (§1.3)."""
    config: AppConfig = deps.config
    settings = config.settings
    est_output = settings.tokens_budget.report_output_max

    async with pool.acquire() as conn:
        used_in, used_out = await token_budget.session_tokens_used(conn, job["session_id"])
    if (
        used_in + est_input > settings.tokens_budget.session_input_budget
        or used_out + est_output > settings.tokens_budget.session_output_budget
    ):
        return None

    upper_bound = token_budget.upper_bound_cost_usd(
        config, input_tokens=est_input, output_tokens=est_output
    )
    provider = "unknown"
    model = "unknown"
    if config.provider_profile is not None:
        provider = config.provider_profile.llm.provider or "unknown"
        model = config.provider_profile.llm.model or "unknown"

    deadline = time.monotonic() + settings.timeouts_seconds.report_deadline_total
    pause_min = settings.limits.provider_retry_pause_min_seconds
    pause_max = settings.limits.provider_retry_pause_max_seconds
    call_index = 0
    while call_index < settings.limits.max_provider_calls_per_operation:
        if time.monotonic() >= deadline:
            return None
        async with pool.acquire() as conn:
            existing = await jobs_repo.count_provider_calls(conn, job["id"])
            if existing >= settings.limits.max_provider_calls_per_operation:
                break
            remaining = await token_budget.daily_budget_remaining(conn, config)
            if remaining is not None and upper_bound > remaining:
                return None
            call_index = existing + 1
            call_id = await token_budget.reserve_call(
                conn,
                job_id=job["id"],
                attempt=call_index,
                operation="report",
                provider=provider,
                model=model,
                profile_version="2.0",
                input_tokens=est_input,
                output_tokens=est_output,
                reserved_usd=upper_bound,
            )
        request = LlmRequest(
            operation="report",
            system_prompt=system_prompt,
            user_payload=user_payload,
            max_output_tokens=est_output,
            temperature=settings.interview.llm_temperature,
            timeout_seconds=settings.timeouts_seconds.report_call,
            json_mode=bool(
                config.provider_profile
                and config.provider_profile.llm.native_json_schema_supported
            ),
        )
        call_started = time.monotonic()
        try:
            result: LlmResult = await deps.llm.complete(request)
        except LlmRetryableError as exc:
            metrics.record_call("report", metrics.outcome_for_error(exc), call_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="known_failure")
            wait = max(0.0, random.uniform(pause_min, pause_max))
            if time.monotonic() + wait >= deadline:
                return None
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="report")
            await asyncio.sleep(wait)
            continue
        except (LlmConfigError, LlmFatalError) as exc:
            metrics.record_call("report", metrics.outcome_for_error(exc), call_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="known_failure")
            return None
        except (TimeoutError, LlmUnknownResultError, OSError) as exc:
            metrics.record_call("report", metrics.outcome_for_error(exc), call_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="unknown")
            if time.monotonic() >= deadline:
                return None
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="report")
            continue
        metrics.record_call("report", metrics.OUTCOME_SUCCESS, call_started)

        actual = token_budget.upper_bound_cost_usd(
            config,
            input_tokens=result.input_tokens if result.input_tokens is not None else est_input,
            output_tokens=result.output_tokens if result.output_tokens is not None else est_output,
        )
        async with pool.acquire() as conn:
            await token_budget.settle_call(
                conn, call_id, outcome="success", actual_usd=actual,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            )

        report = _parse_report(result.content)
        if report is None:
            metrics.REGISTRY.inc(
                metrics.PROVIDER_CALLS, operation="report",
                outcome=metrics.OUTCOME_INVALID_JSON,
            )
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="report")
            continue  # один повтор в рамках лимита двух вызовов
        issues = validate_report(
            report,
            snapshot=snapshot,
            expected_session_id=session_id,
            expected_partial=partial,
        )
        if not issues:
            return report
        metrics.REGISTRY.inc(
            metrics.PROVIDER_CALLS, operation="report",
            outcome=metrics.OUTCOME_INVALID_JSON,
        )
        metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="report")
        logger.info(
            "report validation issues, retry allowed",
            extra={"n": len(issues), "first": issues[0].code},
        )
        continue
    return None


def _parse_report(content: str) -> FeedbackReport | None:
    """Строгий разбор одного JSON-объекта; лишний текст вокруг JSON запрещён
    (§3.4) — никаких code-fence поблажек, в отличие от оценки ответа."""
    text = content.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return FeedbackReport.model_validate(data)
    except ValueError:
        return None


async def _save_and_enqueue(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    owner: str,
    *,
    session: asyncpg.Record,
    report: FeedbackReport,
    parts: list[str],
    partial: bool,
    degraded: bool,
    total_chars: int,
) -> None:
    """Сохранение reports и всех обязательных частей outbox в одной
    транзакции (§2.2). Отчёт сохраняется до отправки и воспроизводится
    без повторного LLM (/report)."""
    async with pool.acquire() as conn, conn.transaction():
        # Канонический порядок блокировок (§2.3): users -> sessions -> jobs
        from app.storage import users_repo

        user = await users_repo.lock_by_id(conn, session["user_id"])
        session_now = await sessions_repo.find_by_id(conn, session["id"], for_update=True)
        job_now = await jobs_repo.lock_for_commit(conn, job["id"])
        if job_now is None or job_now["state"] != "leased" or job_now["lease_owner"] != owner:
            return
        if user is None:
            return
        if session_now is None or session_now["status"] in (
            "completed", "cancelled", "expired", "failed"
        ):
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner, result={"stale": True}
            )
            return
        existing = await conn.fetchrow(
            "SELECT id FROM reports WHERE session_id = $1", session_now["id"]
        )
        if existing is not None:
            report_id = existing["id"]  # идемпотентность: отчёт уже сохранён
        else:
            report_id = await conn.fetchval(
                """
                INSERT INTO reports (session_id, report_json, parts, partial, degraded,
                                     total_chars)
                VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6)
                RETURNING id
                """,
                session_now["id"],
                json.dumps(report.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(parts, ensure_ascii=False),
                partial,
                degraded,
                total_chars,
            )
        for index, part_text in enumerate(parts):
            await outbox_repo.enqueue(
                conn,
                unique_key=f"report:{report_id}:part:{index}",
                user_id=user["id"],
                chat_id=user["chat_id"],
                kind="report_part",
                payload=text_payload(user["chat_id"], part_text),
                session_id=session_now["id"],
                is_required=True,
            )
        await jobs_repo.mark_done(
            conn, job["id"], owner=owner,
            result={"report_id": str(report_id), "parts": len(parts), "degraded": degraded},
        )
        if degraded:
            metrics.REGISTRY.inc(metrics.REPORT_DEGRADED)


async def _finish_job(
    pool: asyncpg.Pool,
    job: asyncpg.Record,
    owner: str,
    state: str,
    *,
    result: dict | None = None,
    error_code: str = "",
) -> None:
    async with pool.acquire() as conn:
        if state == "done":
            await jobs_repo.mark_done(conn, job["id"], owner=owner, result=result or {})
        else:
            await jobs_repo.mark_failed(
                conn, job["id"], owner=owner, error_code=error_code, result=result
            )


# ---------------------------------------------------------------------------
# Доставка: завершение отчёта (§2.2, §2.4, §3.8)
# ---------------------------------------------------------------------------


async def on_report_part_delivered_tx(
    conn: asyncpg.Connection,
    config: AppConfig,
    row: asyncpg.Record,
    message_id: int,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record | None,
) -> None:
    """После подтверждения ВСЕХ частей: completed и перевод резерва попытки
    в consumed — атомарно (§2.2, §3.8)."""
    if session is None:
        return
    report = await conn.fetchrow(
        "SELECT id, parts FROM reports WHERE session_id = $1", session["id"]
    )
    if report is None:
        return
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    total = len(parts)
    delivered = await conn.fetchval(
        """
        SELECT count(*)::int FROM outbox
        WHERE session_id = $1 AND kind = 'report_part' AND state = 'delivered'
          AND unique_key LIKE $2
        """,
        session["id"],
        f"report:{report['id']}:part:%",
    )
    if delivered < total:
        return

    # все части подтверждены — завершение
    await conn.execute(
        "UPDATE reports SET delivered_at = now() WHERE id = $1 AND delivered_at IS NULL",
        report["id"],
    )
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.completed,
        expected_statuses=(SessionStatus.reporting,),
    )
    if updated is None:
        return
    credit = await credits_repo.find_reserved_by_session(conn, session["id"])
    if credit is not None:
        await conn.execute(
            """
            UPDATE credits
            SET state = 'consumed', reserved_session_id = NULL,
                consumed_session_id = $2, updated_at = now()
            WHERE id = $1 AND state = 'reserved'
            """,
            credit["id"],
            session["id"],
        )
        if credit["source"] == "trial":
            await events_repo.emit(
                conn,
                dedup_key=f"trial_consumed:{credit['id']}",
                name="trial_consumed",
                user_id=user["id"],
                session_id=session["id"],
            )
    await events_repo.emit(
        conn,
        dedup_key=f"report_delivered:{session['id']}",
        name="report_delivered",
        user_id=user["id"],
        session_id=session["id"],
        props_category={"degraded": "true" if await _is_degraded(conn, report["id"]) else "false"},
    )
    metrics.REGISTRY.inc(metrics.REPORT_DELIVERED, outcome=metrics.OUTCOME_SUCCESS)
    await conn.execute(
        "UPDATE users SET ui_state = 'idle', version = version + 1 WHERE id = $1",
        user["id"],
    )

    # «Новая тренировка» и необязательная оценка полезности (§3.7)
    ttl = timedelta(hours=config.settings.ui_tokens.question_control_hours)
    buttons = []
    for action, label, allowed in (
        ("report_rating_useful", messages.REPORT_RATING_USEFUL, "useful"),
        ("report_rating_not_useful", messages.REPORT_RATING_NOT_USEFUL, "not_useful"),
        ("new_training", messages.NEW_TRAINING_LABEL, None),
    ):
        token = generate_ui_token()
        await ui_tokens_repo.issue(
            conn,
            token=token,
            user_id=user["id"],
            action=action,
            ttl=ttl,
            allowed_value=allowed,
            session_id=session["id"],
        )
        buttons.append([(label, f"{action}:{token}")])
    await outbox_repo.enqueue(
        conn,
        unique_key=f"report-footer:{session['id']}",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="text",
        payload=text_payload(user["chat_id"], messages.REPORT_DELIVERED_FOOTER),
        session_id=session["id"],
    )
    await outbox_repo.enqueue(
        conn,
        unique_key=f"report-rating:{session['id']}",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="buttons",
        payload=buttons_payload(user["chat_id"], messages.REPORT_RATING_PROMPT, buttons),
        session_id=session["id"],
    )


async def _is_degraded(conn: asyncpg.Connection, report_id: uuid.UUID) -> bool:
    return bool(
        await conn.fetchval("SELECT degraded FROM reports WHERE id = $1", report_id)
    )


async def mark_delivery_started(
    conn: asyncpg.Connection, session_id: uuid.UUID
) -> None:
    """Перед первой попыткой отправки отчёта ставится delivery_started_at;
    после этого отмена возвращает BUSY (§2.4)."""
    await conn.execute(
        """
        UPDATE reports SET delivery_started_at = now()
        WHERE session_id = $1 AND delivery_started_at IS NULL
        """,
        session_id,
    )
