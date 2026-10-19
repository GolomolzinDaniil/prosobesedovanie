"""Обработка assess-заданий: оценка ответа и адаптивный переход (§2.2, §3.2, §3.3; T11, T12).

Фаза A (без удержания блокировок БД): построение GenerateTurnInput из
сохранённых данных, проверка токенов и бюджета, вызовы провайдера (не более
двух на операцию), строгая валидация JSON и семантики (цитаты, пары оценок).

Фаза B (транзакция, канонический порядок блокировок users -> sessions ->
turns -> jobs): сравнение ожидаемой версии, сохранение оценки, решение
сервера по фиксированному правилу (уточнение/следующая тема/отчёт), шаг
сложности, создание следующего вопроса (вопрос модели принимается только
валидным, иначе вопрос каталога), либо запасной сценарий при недоступности
AI. Поздний результат устаревшей версии отбрасывается (§2.4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.config import AppConfig, CatalogEntry
from app.providers.llm import (
    LlmClient,
    LlmConfigError,
    LlmFatalError,
    LlmRequest,
    LlmRetryableError,
    LlmUnknownResultError,
)
from app.services import metrics, session_service, token_budget
from app.services.token_budget import estimate_tokens
from app.storage import (
    jobs_repo,
    outbox_repo,
    sessions_repo,
    turns_repo,
    users_repo,
)
from app.telegram import messages
from contracts.models import (
    ErrorCode,
    GenerateTurnInput,
    PreviousAssessmentBrief,
    QuestionType,
    SessionStatus,
    TurnAssessment,
    TurnKind,
    TurnScores,
)
from contracts.semantic_validation import (
    compute_allowed_action,
    compute_next_difficulty,
    validate_assessment,
)

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "cancelled", "expired", "failed")

# Стоп-слова для тематической эвристики (существенные слова >= 4 символов)
_TOPIC_STOPWORDS = frozenset((
    "какие", "какой", "какая", "какое", "расскажите", "объясните", "почему",
    "чем", "как", "что", "такое", "этого", "этой", "этом", "чтобы", "если",
    "вам", "вас", "вами", "они", "данный", "данная", "вашей", "вашего",
    "привести", "пример", "примеры", "случае", "случай", "задача", "задачи",
    "ответ", "ответа", "ответов", "ответы", "использовать", "используете",
    "использует", "используют", "применение", "применения", "примените",
    "вашу", "ваших", "тебе", "тебя", "ваш", "ваша", "ваши",
))


@dataclass
class AssessOutcome:
    """Итог фазы A: валидная оценка либо запасной сценарий с причиной."""

    assessment: TurnAssessment | None
    error_code: ErrorCode | None
    detail: str = ""

    @classmethod
    def ok(cls, assessment: TurnAssessment) -> AssessOutcome:
        return cls(assessment=assessment, error_code=None)

    @classmethod
    def fallback(cls, code: ErrorCode, detail: str = "") -> AssessOutcome:
        return cls(assessment=None, error_code=code, detail=detail)

    @property
    def is_fallback(self) -> bool:
        return self.assessment is None


# ---------------------------------------------------------------------------
# Построение входа модели (§3.3, prompts/user-envelope.txt)
# ---------------------------------------------------------------------------


def _significant_words(text: str) -> set[str]:
    """Существенные слова для тематической проверки: длина >= 4, без
    стоп-слов, с усечением основы до 5 символов (русские словоформы)."""
    normalized = unicodedata.normalize("NFC", text.lower())
    words = re.findall(r"[a-zа-яё0-9]{4,}", normalized)
    return {w[:5] for w in words if w not in _TOPIC_STOPWORDS}


def question_matches_topic(question: str, entry: CatalogEntry) -> bool:
    """Тематическая эвристика следующего вопроса (§3.3): существенные слова
    вопроса пересекаются с лексикой предписанной темы каталога. Недостаточное
    пересечение -> вопрос отбрасывается, используется вопрос каталога.
    Полная предметная проверка — тесты T21 с рецензентом."""
    target_text = " ".join(
        [entry.fallback_question, " ".join(entry.rubric.key_points)]
        + list((entry.difficulty_variants or {}).values())
        + ([entry.question] if entry.question else [])
    )
    target = _significant_words(target_text)
    question_words = _significant_words(question)
    if not question_words or not target:
        return False
    return len(question_words & target) >= 2


@dataclass
class AssessContext:
    user: asyncpg.Record
    session: asyncpg.Record
    turn: asyncpg.Record
    previous: list[asyncpg.Record]
    next_entry: CatalogEntry | None


async def _load_context(
    conn: asyncpg.Connection, config: AppConfig, job: asyncpg.Record
) -> AssessContext | None:
    session = await sessions_repo.find_by_id(conn, job["session_id"])
    if session is None:
        return None
    user = await users_repo.lock_by_id(conn, session["user_id"])  # read within tx later
    turn = await turns_repo.find_by_id(conn, job["turn_id"])
    if user is None or turn is None:
        return None
    previous = [
        t
        for t in await turns_repo.list_by_session(conn, session["id"])
        if t["assessment"] is not None and t["id"] != turn["id"]
    ]
    scenario = session_service.scenario_for(config, session["role_id"], session["level"])
    base_index = turn["main_index"]
    if turn["kind"] == TurnKind.follow_up.value and turn["parent_turn_id"] is not None:
        parent = await turns_repo.find_by_id(conn, turn["parent_turn_id"])
        base_index = parent["main_index"] if parent else 1
    next_entry = None
    if base_index is not None and base_index < config.settings.interview.main_questions:
        next_entry = session_service.entry_by_main_index(scenario, base_index + 1)
    return AssessContext(
        user=user, session=session, turn=turn, previous=previous, next_entry=next_entry
    )


def build_generate_turn_input(
    config: AppConfig, ctx: AssessContext
) -> tuple[GenerateTurnInput, str, str]:
    """GenerateTurnInput + (system_prompt, user_payload). История сокращается
    по старым объяснениям, не по текущему ответу (§3.3)."""
    turn, session = ctx.turn, ctx.session
    question_type = QuestionType(turn["question_type"])
    rubric = turn["rubric"]
    if isinstance(rubric, str):
        rubric = json.loads(rubric)

    briefs: list[PreviousAssessmentBrief] = []
    for row in ctx.previous:
        assessment = row["assessment"]
        if isinstance(assessment, str):
            assessment = json.loads(assessment)
        briefs.append(
            PreviousAssessmentBrief(
                turn_id=row["id"],
                topic=row["topic"],
                scores=TurnScores.model_validate(assessment["scores"]),
                missing_points=list(assessment.get("missing_points") or []),
                brief_explanation=(assessment.get("explanation") or "")[:300] or None,
            )
        )
    # порядок хронологический (по ordinal); сокращение — самыми старыми

    follow_ups_remaining = 0
    if turn["kind"] == TurnKind.main.value:
        follow_ups_remaining = max(
            0,
            config.settings.interview.max_follow_ups_per_session - session["follow_ups_used"],
        )

    level = session["level"]
    data = GenerateTurnInput(
        session_id=session["id"],
        turn_id=turn["id"],
        role_id=session["role_id"],
        level=level,
        kind=TurnKind(turn["kind"]),
        question_type=question_type,
        topic=turn["topic"],
        question=turn["question_text"],
        rubric=rubric,
        answer=turn["answer_text"],
        previous_assessments=briefs,
        next_allowed_topic=(ctx.next_entry.topic if ctx.next_entry else None),
        follow_ups_remaining=follow_ups_remaining,
        current_difficulty=session["current_difficulty"],
        difficulty_min=config.settings.interview.difficulty_min[level],
        difficulty_max=config.settings.interview.difficulty_max[level],
    )
    system_prompt = config.prompts["turn-system.txt"]

    def payload_of(d: GenerateTurnInput) -> str:
        return json.dumps(d.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))

    user_payload = payload_of(data)
    limit = config.settings.tokens_budget.assess_input_max

    # Сокращение истории: сначала старые объяснения, затем самые старые оценки
    while estimate_tokens(system_prompt + user_payload) > limit and any(
        b.brief_explanation for b in data.previous_assessments
    ):
        for i, brief in enumerate(data.previous_assessments):
            if brief.brief_explanation:
                data.previous_assessments[i] = brief.model_copy(
                    update={"brief_explanation": None}
                )
                break
        user_payload = payload_of(data)
    while estimate_tokens(system_prompt + user_payload) > limit and data.previous_assessments:
        data.previous_assessments.pop(0)
        user_payload = payload_of(data)

    return data, system_prompt, user_payload


# ---------------------------------------------------------------------------
# Разбор и валидация результата модели (§3.3)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_assessment_json(content: str) -> dict[str, Any]:
    """Строгий разбор одного JSON-объекта; текст модели недоверенный.

    Допускается снятие code-fence обёртки (для отчёта §3.4 лишний текст
    запрещён жёстче — там разбор отдельный).
    """
    text = content.strip()
    text = _FENCE_RE.sub("", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("результат модели не является JSON-объектом")
    return data


def validate_assessment_payload(
    data: dict[str, Any], *, expected_turn_id: uuid.UUID, question_type: QuestionType,
    answer_text: str,
) -> TurnAssessment:
    """Структурная + семантическая валидация; бросает ValueError/ValidationError
    с описанием проблемы (вызывающий код считает вывод невалидным)."""
    assessment = TurnAssessment.model_validate(data)
    if assessment.turn_id != expected_turn_id:
        raise ValueError("turn_id оценки не совпадает с оцениваемым вопросом")
    issues = validate_assessment(
        assessment, question_type=question_type, answer_text=answer_text
    )
    if issues:
        descriptions = "; ".join(f"{i.code}: {i.message}" for i in issues[:3])
        raise ValueError(f"семантические проблемы оценки: {descriptions}")
    return assessment


# ---------------------------------------------------------------------------
# Фаза A: вызовы провайдера (§3.2)
# ---------------------------------------------------------------------------


async def run_assessment(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str
) -> None:
    outcome = await _phase_a(pool, deps, job)
    await _phase_b(pool, deps, job, owner, outcome)


async def apply_direct_fallback(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str, code: ErrorCode, detail: str
) -> None:
    """Запасной сценарий без вызова провайдера (очередь/бюджет/конфигурация)."""
    await _phase_b(pool, deps, job, owner, AssessOutcome.fallback(code, detail))


async def _phase_a(pool: asyncpg.Pool, deps: Any, job: asyncpg.Record) -> AssessOutcome:
    config: AppConfig = deps.config
    settings = config.settings
    llm: LlmClient | None = deps.llm

    async with pool.acquire() as conn:
        ctx = await _load_context(conn, config, job)
    if ctx is None:
        return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "context_missing")

    # AI заблокирован профилем (§1.4) — запасной сценарий без вызовов
    if llm is None or not config.ai_enabled:
        return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "ai_disabled")

    _data, system_prompt, user_payload = build_generate_turn_input(config, ctx)
    est_input = estimate_tokens(system_prompt + user_payload)
    est_output = settings.tokens_budget.assess_output_max

    # Токен-бюджет сессии с учётом резерва отчёта (§1.3)
    async with pool.acquire() as conn:
        used_in, used_out = await token_budget.session_tokens_used(conn, ctx.session["id"])
    if not token_budget.check_session_token_budget(
        config,
        used_input=used_in,
        used_output=used_out,
        planned_input=est_input,
        planned_output=est_output,
    ):
        return AssessOutcome.fallback(ErrorCode.BUDGET_EXCEEDED, "session_token_budget")

    # Суточный денежный лимит блокирует новые дорогие задания (§3.10)
    upper_bound = token_budget.upper_bound_cost_usd(
        config, input_tokens=est_input, output_tokens=est_output
    )
    async with pool.acquire() as conn:
        remaining = await token_budget.daily_budget_remaining(conn, config)
    if remaining is not None and upper_bound > remaining:
        return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "daily_budget_exceeded")

    provider_name = "unknown"
    model_name = "unknown"
    if config.provider_profile is not None:
        provider_name = config.provider_profile.llm.provider or "unknown"
        model_name = config.provider_profile.llm.model or "unknown"

    deadline = time.monotonic() + settings.timeouts_seconds.assessment_deadline_total
    pause_min = settings.limits.provider_retry_pause_min_seconds
    pause_max = settings.limits.provider_retry_pause_max_seconds
    invalid_json_seen = False
    call_index = 0

    while call_index < settings.limits.max_provider_calls_per_operation:
        # резерв стоимости транзакционно до внешнего вызова (§3.10)
        async with pool.acquire() as conn:
            existing_calls = await jobs_repo.count_provider_calls(conn, job["id"])
            if existing_calls >= settings.limits.max_provider_calls_per_operation:
                break
            call_index = existing_calls + 1
            call_id = await token_budget.reserve_call(
                conn,
                job_id=job["id"],
                attempt=call_index,
                operation="assess",
                provider=provider_name,
                model=model_name,
                profile_version="2.0",
                input_tokens=est_input,
                output_tokens=est_output,
                reserved_usd=upper_bound,
            )

        request = LlmRequest(
            operation="assess",
            system_prompt=system_prompt,
            user_payload=user_payload,
            max_output_tokens=est_output,
            temperature=settings.interview.llm_temperature,
            timeout_seconds=settings.timeouts_seconds.assess_call,
            json_mode=bool(
                config.provider_profile
                and config.provider_profile.llm.native_json_schema_supported
            ),
        )

        call_started = time.monotonic()
        try:
            result = await llm.complete(request)
        except LlmRetryableError as exc:
            metrics.record_call("assess", metrics.outcome_for_error(exc), call_started)
            await _settle(pool, call_id, "known_failure")
            wait = _retry_wait(exc.retry_after_seconds, deadline, pause_min, pause_max)
            if wait is None:
                return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "retry_deadline")
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="assess")
            await asyncio.sleep(wait)
            continue
        except LlmConfigError as exc:
            metrics.record_call("assess", metrics.outcome_for_error(exc), call_started)
            await _settle(pool, call_id, "known_failure")
            # Сигнал оператору: 401/403 AI (§3.10)
            logger.error("llm config error: provider rejected credentials")
            return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "config_error")
        except LlmFatalError as exc:
            metrics.record_call("assess", metrics.outcome_for_error(exc), call_started)
            await _settle(pool, call_id, "known_failure")
            return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "fatal_error")
        except LlmUnknownResultError as exc:
            metrics.record_call("assess", metrics.outcome_for_error(exc), call_started)
            # Неизвестный результат учитывается как unknown_usd до сверки (§3.10)
            await _settle(pool, call_id, "unknown")
            if time.monotonic() >= deadline:
                return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "unknown_deadline")
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="assess")
            await asyncio.sleep(max(0.0, random.uniform(pause_min, pause_max)))
            continue
        except (TimeoutError, OSError) as exc:
            metrics.record_call("assess", metrics.outcome_for_error(exc), call_started)
            await _settle(pool, call_id, "unknown")
            logger.warning("llm call error", extra={"error": type(exc).__name__})
            if time.monotonic() >= deadline:
                return AssessOutcome.fallback(ErrorCode.AI_UNAVAILABLE, "call_deadline")
            continue
        metrics.record_call("assess", metrics.OUTCOME_SUCCESS, call_started)

        # Успех: резерв заменяется фактической стоимостью
        actual = token_budget.upper_bound_cost_usd(
            config,
            input_tokens=result.input_tokens if result.input_tokens is not None else est_input,
            output_tokens=result.output_tokens if result.output_tokens is not None else est_output,
        )
        await _settle(
            pool,
            call_id,
            "success",
            actual_usd=actual,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

        try:
            payload = parse_assessment_json(result.content)
            assessment = validate_assessment_payload(
                payload,
                expected_turn_id=ctx.turn["id"],
                question_type=QuestionType(ctx.turn["question_type"]),
                answer_text=ctx.turn["answer_text"],
            )
        except ValueError as exc:
            # pydantic ValidationError наследует ValueError; JSONDecodeError тоже
            invalid_json_seen = True
            metrics.REGISTRY.inc(
                metrics.PROVIDER_CALLS, operation="assess",
                outcome=metrics.OUTCOME_INVALID_JSON,
            )
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="assess")
            logger.info(
                "invalid model output, retry allowed",
                extra={"job": str(job["id"]), "reason": str(exc)[:200]},
            )
            continue
        return AssessOutcome.ok(assessment)

    code = ErrorCode.AI_INVALID_OUTPUT if invalid_json_seen else ErrorCode.AI_UNAVAILABLE
    return AssessOutcome.fallback(code, "calls_exhausted")


def _retry_wait(
    retry_after: int | None, deadline: float, pause_min: int, pause_max: int
) -> float | None:
    """Пауза повтора: retry_after учитывается только если укладывается в
    дедлайн; иначе 2–5 секунд (§3.2). None — повтор не выполняется."""
    now = time.monotonic()
    if retry_after is not None:
        if now + retry_after <= deadline:
            return float(retry_after)
        return None
    if now + pause_min > deadline:
        return None
    return max(0.0, random.uniform(pause_min, pause_max))


async def _settle(
    pool: asyncpg.Pool,
    call_id: uuid.UUID,
    outcome: str,
    *,
    actual_usd: Any = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    async with pool.acquire() as conn:
        await token_budget.settle_call(
            conn,
            call_id,
            outcome=outcome,
            actual_usd=actual_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ---------------------------------------------------------------------------
# Фаза B: применение под блокировками (§2.2, §2.4)
# ---------------------------------------------------------------------------


async def _phase_b(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str, outcome: AssessOutcome
) -> None:
    config: AppConfig = deps.config
    async with pool.acquire() as conn, conn.transaction():
        # Канонический порядок блокировок (§2.3): users -> sessions -> turns -> jobs.
        # Чтение сессии до блокировок — только для навигации (без FOR UPDATE).
        session_pre = await sessions_repo.find_by_id(conn, job["session_id"])
        if session_pre is None:
            return  # сессия удалена каскадом — задание тоже
        user = await users_repo.lock_by_id(conn, session_pre["user_id"])
        if user is None:
            return
        session = await sessions_repo.find_by_id(conn, job["session_id"], for_update=True)
        if session is None:  # pragma: no cover - гонка удаления
            return
        job_now = await jobs_repo.lock_for_commit(conn, job["id"])
        if (
            job_now is None
            or job_now["state"] != "leased"
            or job_now["lease_owner"] != owner
        ):
            # задание отменено или lease перехвачен — результат не применяем:
            # два worker не могут применить один результат (§3.2)
            return
        if session["status"] in TERMINAL or session["status"] != SessionStatus.evaluating.value:
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner, result={"stale": True, "reason": "status"}
            )
            return
        if session["version"] != job["expected_version"]:
            # поздний результат устаревшей версии отбрасывается (§2.4)
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner, result={"stale": True, "reason": "version"}
            )
            return
        turn = await turns_repo.find_by_id(conn, job["turn_id"], for_update=True)
        if (
            turn is None
            or turn["answer_text"] is None
            or turn["assessment"] is not None
            or session["current_turn_id"] != turn["id"]
        ):
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner, result={"stale": True, "reason": "turn"}
            )
            return

        if user["deletion_requested_at"] is not None:
            # AI-результат повторно проверяет признак удаления перед записью (§2.2)
            await jobs_repo.mark_failed(
                conn, job["id"], owner=owner, error_code="DELETION_IN_PROGRESS"
            )
            return

        if outcome.is_fallback:
            await _apply_fallback(
                conn, config, user=user, session=session, turn=turn, job=job,
                owner=owner, outcome=outcome,
            )
            return
        await _apply_assessment(
            conn, config, user=user, session=session, turn=turn, job=job,
            owner=owner, assessment=outcome.assessment,
        )


async def _apply_assessment(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    job: asyncpg.Record,
    owner: str,
    assessment: TurnAssessment,
) -> None:
    """Сохранение оценки и переход по фиксированному правилу сервера (§3.3)."""
    await conn.execute(
        "UPDATE turns SET assessment = $2::jsonb, assessed_at = now() WHERE id = $1",
        turn["id"],
        json.dumps(assessment.model_dump(mode="json"), ensure_ascii=False),
    )

    question_type = QuestionType(turn["question_type"])
    is_main = turn["kind"] == TurnKind.main.value
    follow_ups_remaining = (
        max(
            0,
            config.settings.interview.max_follow_ups_per_session - session["follow_ups_used"],
        )
        if is_main
        else 0
    )
    parent_has_follow_up = (not is_main) or await turns_repo.has_follow_up(conn, turn["id"])
    allowed = compute_allowed_action(
        question_type=question_type,
        scores=assessment.scores,
        missing_points=assessment.missing_points,
        follow_ups_remaining=follow_ups_remaining,
        parent_has_follow_up=parent_has_follow_up,
        answer_text=turn["answer_text"],
    )
    # Мнение модели принимается только если совпадает с допустимым действием;
    # иначе действие сервера имеет приоритет, next_question отбрасывается (§3.3)
    model_opinion_accepted = assessment.suggested_action.value == allowed.value

    # «Завершить» во время оценки: новая тема не создаётся (§2.4)
    if session["requested_action"] == "finish":
        await _goto_reporting(conn, config, user=user, session=session, job=job, owner=owner,
                              decision="finish_requested")
        return

    # Шаг сложности — после основного технического ответа (§3.3): применяется
    # независимо от того, создаётся уточнение или следующая основная тема.
    # Уточнение использует сложность родителя (turn.difficulty), а не сессии.
    new_difficulty = session["current_difficulty"]
    if is_main:
        level = session["level"]
        new_difficulty = compute_next_difficulty(
            current=session["current_difficulty"],
            difficulty_min=config.settings.interview.difficulty_min[level],
            difficulty_max=config.settings.interview.difficulty_max[level],
            question_type=question_type,
            is_main_answer=True,
            scores=assessment.scores,
        )
        if new_difficulty != session["current_difficulty"]:
            await sessions_repo.set_difficulty(conn, session["id"], new_difficulty)

    if allowed.value == "follow_up" and is_main and model_opinion_accepted:
        next_question = assessment.next_question or ""
        issues = _validate_model_question(next_question)
        if not issues:
            await _create_follow_up(
                conn, config, user=user, session=session, parent=turn,
                question_text=next_question, job=job, owner=owner,
            )
            return
        # невалидный вопрос уточнения — переход к следующей основной теме
        logger.info("follow-up question rejected", extra={"issues": issues[:2]})

    # Следующая основная тема либо конец плана
    base_index = turn["main_index"]
    if not is_main and turn["parent_turn_id"] is not None:
        parent = await turns_repo.find_by_id(conn, turn["parent_turn_id"])
        base_index = parent["main_index"] if parent else base_index
    next_index = (base_index or 1) + 1
    if next_index > config.settings.interview.main_questions:
        await _goto_reporting(conn, config, user=user, session=session, job=job, owner=owner,
                              decision="plan_complete")
        return

    scenario = session_service.scenario_for(config, session["role_id"], session["level"])
    next_entry = session_service.entry_by_main_index(scenario, next_index)

    question_override: str | None = None
    if (
        model_opinion_accepted
        and next_entry.question_type is QuestionType.technical
        and assessment.next_question
    ):
        issues = _validate_model_question(assessment.next_question)
        if not issues and question_matches_topic(assessment.next_question, next_entry):
            question_override = assessment.next_question
        else:
            logger.info(
                "next question rejected, catalog fallback",
                extra={"issues": (issues or ["topic_mismatch"])[:2]},
            )

    await session_service.advance_to_main(
        conn,
        config,
        user=user,
        session=session,
        main_index=next_index,
        notice=None,
        notice_key=None,
        question_override=question_override,
        difficulty_override=(
            new_difficulty if next_entry.question_type is QuestionType.technical else None
        ),
        expected_from=(SessionStatus.evaluating,),
        touch_user_action=False,
    )
    await jobs_repo.mark_done(
        conn,
        job["id"],
        owner=owner,
        result={
            "decision": "next_main",
            "action": allowed.value,
            "model_opinion_accepted": model_opinion_accepted,
            "question_source": "model" if question_override else "catalog",
            "difficulty": new_difficulty,
        },
    )


def _validate_model_question(question: str) -> list[str]:
    """Отклонить вопрос пустой, длиннее 700 или с управляющими символами (§3.3)."""
    issues: list[str] = []
    if not question or not question.strip():
        issues.append("empty")
        return issues
    if len(question) > 700:
        issues.append("too_long")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n" for ch in question):
        issues.append("control_chars")
    return issues


async def _create_follow_up(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    parent: asyncpg.Record,
    question_text: str,
    job: asyncpg.Record,
    owner: str,
) -> None:
    """Уточнение: та же тема, сложность родителя, ≤1 на основной вопрос и
    ≤2 на сессию (§1.3, §3.3)."""
    ordinal = await turns_repo.next_ordinal(conn, session["id"])
    rubric = parent["rubric"]
    if isinstance(rubric, str):
        rubric = json.loads(rubric)
    turn = await turns_repo.create_follow_up_turn(
        conn,
        session_id=session["id"],
        ordinal=ordinal,
        parent_turn_id=parent["id"],
        question_type=parent["question_type"],
        topic=parent["topic"],
        question_text=question_text,
        rubric=rubric,
        catalog_entry_id=parent["catalog_entry_id"],
        difficulty=parent["difficulty"],
    )
    await conn.execute(
        "UPDATE sessions SET follow_ups_used = follow_ups_used + 1 WHERE id = $1",
        session["id"],
    )
    await sessions_repo.set_current_turn(conn, session["id"], turn["id"])
    updated = await sessions_repo.transition(
        conn,
        session["id"],
        SessionStatus.delivering_question,
        expected_statuses=(SessionStatus.evaluating,),
    )
    await session_service.enqueue_question_with_controls(
        conn, config, user=user, session_id=session["id"], session=updated, turn=turn
    )
    await jobs_repo.mark_done(
        conn, job["id"], owner=owner, result={"decision": "follow_up", "turn_id": str(turn["id"])}
    )


async def _goto_reporting(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    job: asyncpg.Record,
    owner: str,
    decision: str,
) -> None:
    answered = await turns_repo.count_answered(conn, session["id"])
    if answered == 0:
        # Недостижимо при обычной оценке (ответ принят), защита по NO_ANSWERS
        await session_service.cancel_without_answers(conn, config, user=user, session=session)
        await jobs_repo.mark_done(
            conn, job["id"], owner=owner, result={"decision": "no_answers"}
        )
        return
    await session_service.start_reporting_tx(
        conn, config, user=user, session=session, touch_user_action=False
    )
    await jobs_repo.mark_done(
        conn, job["id"], owner=owner, result={"decision": decision, "reporting": True}
    )


async def _apply_fallback(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    job: asyncpg.Record,
    owner: str,
    outcome: AssessOutcome,
) -> None:
    """Запасной сценарий (§3.2): оценка отсутствует (assessment=NULL),
    интервью продолжается вопросом из каталога; при BUDGET_EXCEEDED —
    готовим разбор уже полученных ответов."""
    code = outcome.error_code or ErrorCode.AI_UNAVAILABLE
    metrics.REGISTRY.inc(metrics.FALLBACK, operation="assess", reason=code.value)
    from app.domain.errors import ERROR_REGISTRY

    notice_text = ERROR_REGISTRY[code][0]

    if code is ErrorCode.BUDGET_EXCEEDED:
        answered = await turns_repo.count_answered(conn, session["id"])
        if answered == 0:
            await session_service.cancel_without_answers(conn, config, user=user, session=session)
        else:
            await session_service.start_reporting_tx(
                conn, config, user=user, session=session, touch_user_action=False
            )
        await session_service.enqueue_notice(
            conn, user=user, text=notice_text, unique_key=f"ai-fallback:{turn['id']}",
            session_id=session["id"],
        )
        await jobs_repo.mark_failed(
            conn, job["id"], owner=owner, error_code=code.value,
            result={"fallback": True, "detail": outcome.detail, "decision": "reporting"},
        )
        return

    # AI_UNAVAILABLE / AI_INVALID_OUTPUT: следующий вопрос из каталога
    await session_service.enqueue_notice(
        conn, user=user, text=notice_text, unique_key=f"ai-fallback:{turn['id']}",
        session_id=session["id"],
    )
    if session["requested_action"] == "finish":
        await _goto_reporting(
            conn, config, user=user, session=session, job=job, owner=owner,
            decision="finish_requested_fallback",
        )
        await jobs_repo.mark_failed(
            conn, job["id"], owner=owner, error_code=code.value,
            result={"fallback": True, "detail": outcome.detail},
        )
        return

    base_index = turn["main_index"]
    if turn["kind"] == TurnKind.follow_up.value and turn["parent_turn_id"] is not None:
        parent = await turns_repo.find_by_id(conn, turn["parent_turn_id"])
        base_index = parent["main_index"] if parent else base_index
    next_index = (base_index or 1) + 1
    if next_index > config.settings.interview.main_questions:
        await _goto_reporting(
            conn, config, user=user, session=session, job=job, owner=owner,
            decision="plan_complete_fallback",
        )
        await jobs_repo.mark_failed(
            conn, job["id"], owner=owner, error_code=code.value,
            result={"fallback": True, "detail": outcome.detail},
        )
        return

    await session_service.advance_to_main(
        conn,
        config,
        user=user,
        session=session,
        main_index=next_index,
        notice=None,
        notice_key=None,
        expected_from=(SessionStatus.evaluating,),
        touch_user_action=False,
    )
    await jobs_repo.mark_failed(
        conn, job["id"], owner=owner, error_code=code.value,
        result={"fallback": True, "detail": outcome.detail, "decision": "catalog_next"},
    )


# ---------------------------------------------------------------------------
# Обновление статусного сообщения ожидания (§3.7: после 15 секунд, один раз)
# ---------------------------------------------------------------------------


async def loading_update_task(
    pool: asyncpg.Pool,
    deps: Any,
    *,
    turn_id: uuid.UUID | None,
    chat_id: int,
    stop: asyncio.Event,
    unique_key: str | None = None,
) -> None:
    delay = deps.config.settings.ui.loading_update_after_seconds
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
        return  # обработка завершилась раньше порога
    except TimeoutError:
        pass
    if stop.is_set():
        return
    key = unique_key or (f"ack:{turn_id}" if turn_id else None)
    if key is None:
        return
    async with pool.acquire() as conn:
        row = await outbox_repo.get_by_unique_key(conn, key)
    if row is not None and row["message_id"] is not None and row["state"] == "delivered":
        await deps.bot.edit_message_text(
            chat_id, row["message_id"], messages.PROCESSING_TAKES_LONGER
        )
