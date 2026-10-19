"""Токен-контроль и денежный бюджет AI-вызовов (§1.3, §3.2, §3.10).

- Объём входа проверяется до отправки; до подключения tokenizer выбранного
  провайдера (поле tokenizer_reference в профиле, smoke T01) используется
  консервативная оценка: для русскоязычного текста cl-подобные токенизаторы
  дают ~2 символа на токен, оценка len/2 завышает объём и безопасна для
  лимитов. Профиль провайдера может заменить оценку фактическим токенизатором.
- Бюджет сессии: до 100000 входных и 22000 выходных токенов включая повторы;
  для отчёта заранее сохраняется резерв 24000/7000 — оценка не может его
  потратить (§1.3). При нехватке — BUDGET_EXCEEDED: «Подготовим разбор уже
  полученных ответов».
- Деньги: резерв транзакционно до вызова по верхней границе; успех заменяет
  резерв фактической стоимостью; известный нетарифицированный отказ освобождает
  (0); неизвестный результат учитывается как unknown_usd до сверки (§3.10).
- Суточный лимит блокирует новые дорогие задания, сохраняя права пользователя
  и выдавая доступный запасной результат.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import asyncpg

from app.config import AppConfig


def estimate_tokens(text: str) -> int:
    """Консервативная оценка токенов до подключения токенизатора провайдера."""
    return max(1, (len(text) + 1) // 2)


async def session_tokens_used(
    conn: asyncpg.Connection, session_id: uuid.UUID
) -> tuple[int, int]:
    """Суммарные учтённые токены сессии (включая повторы, §1.3)."""
    row = await conn.fetchrow(
        """
        SELECT COALESCE(sum(c.input_tokens_estimated), 0)::bigint AS input_total,
               COALESCE(sum(c.output_tokens_estimated), 0)::bigint AS output_total
        FROM ai_calls c
        JOIN jobs j ON j.id = c.job_id
        WHERE j.session_id = $1
        """,
        session_id,
    )
    return int(row["input_total"]), int(row["output_total"])


def check_session_token_budget(
    config: AppConfig,
    *,
    used_input: int,
    used_output: int,
    planned_input: int,
    planned_output: int,
) -> bool:
    """Хватает ли бюджета сессии на вызов с учётом зарезервированного отчёта."""
    tb = config.settings.tokens_budget
    available_input = tb.session_input_budget - tb.report_reserve_input
    available_output = tb.session_output_budget - tb.report_reserve_output
    return (
        used_input + planned_input <= available_input
        and used_output + planned_output <= available_output
    )


def _prices(config: AppConfig) -> tuple[Decimal, Decimal]:
    costs = config.settings.costs
    profile = config.provider_profile
    price_in = costs.llm_input_per_1k_usd
    price_out = costs.llm_output_per_1k_usd
    if profile is not None and profile.llm is not None:
        extra = profile.llm.model_extra or {}
        price_in = price_in if price_in is not None else extra.get("price_input_per_1k_usd")
        price_out = price_out if price_out is not None else extra.get("price_output_per_1k_usd")
    return (
        Decimal(str(price_in)) if price_in is not None else Decimal(0),
        Decimal(str(price_out)) if price_out is not None else Decimal(0),
    )


def upper_bound_cost_usd(
    config: AppConfig, *, input_tokens: int, output_tokens: int
) -> Decimal:
    """Верхняя граница стоимости запроса и ответа (для резерва, §3.10)."""
    price_in, price_out = _prices(config)
    return (
        Decimal(input_tokens) / Decimal(1000) * price_in
        + Decimal(output_tokens) / Decimal(1000) * price_out
    ).quantize(Decimal("0.000001"))


async def daily_budget_remaining(
    conn: asyncpg.Connection, config: AppConfig, day: date | None = None
) -> Decimal | None:
    """Остаток суточного бюджета; None если бюджет не задан."""
    budget = config.settings.costs.daily_ai_budget_usd
    if budget is None:
        return None
    today = day or datetime.now(UTC).date()
    row = await conn.fetchrow(
        """
        SELECT reserved_usd + known_usd + unknown_usd AS spent
        FROM daily_usage WHERE day = $1
        """,
        today,
    )
    spent = row["spent"] if row else Decimal(0)
    return Decimal(str(budget)) - Decimal(str(spent))


async def reserve_call(
    conn: asyncpg.Connection,
    *,
    job_id: uuid.UUID,
    attempt: int,
    operation: str,
    provider: str,
    model: str,
    profile_version: str,
    input_tokens: int,
    output_tokens: int,
    reserved_usd: Decimal,
) -> uuid.UUID:
    """Транзакционный резерв стоимости до внешнего вызова (§3.10).

    UNIQUE(job_id, attempt) защищает от двойного резерва при повторе обработки.
    """
    call_id = await conn.fetchval(
        """
        INSERT INTO ai_calls (job_id, attempt, operation, provider, model,
                              profile_version, input_tokens_estimated,
                              output_tokens_estimated, reserved_usd, result_state)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'unknown')
        ON CONFLICT (job_id, attempt) DO UPDATE SET reserved_usd = EXCLUDED.reserved_usd
        RETURNING id
        """,
        job_id,
        attempt,
        operation,
        provider,
        model,
        profile_version,
        input_tokens,
        output_tokens,
        reserved_usd,
    )
    await conn.execute(
        """
        INSERT INTO daily_usage (day, reserved_usd)
        VALUES (CURRENT_DATE, $1)
        ON CONFLICT (day) DO UPDATE
        SET reserved_usd = daily_usage.reserved_usd + EXCLUDED.reserved_usd,
            updated_at = now()
        """,
        reserved_usd,
    )
    from app.services import metrics

    metrics.REGISTRY.inc(
        metrics.COST_RESERVED_USD, metrics.micros(reserved_usd), operation=operation
    )
    return call_id


async def settle_call(
    conn: asyncpg.Connection,
    call_id: uuid.UUID,
    *,
    outcome: str,
    actual_usd: Decimal | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Итог вызова: success заменяет резерв фактом; known_failure освобождает
    (0); unknown оставляет резерв списанным в unknown_usd до сверки (§3.10)."""
    if outcome not in ("success", "known_failure", "unknown"):
        raise ValueError(f"неизвестный исход {outcome}")
    row = await conn.fetchrow(
        "SELECT reserved_usd FROM ai_calls WHERE id = $1 FOR UPDATE", call_id
    )
    if row is None:
        return
    reserved = Decimal(str(row["reserved_usd"]))
    if outcome == "success":
        actual = reserved if actual_usd is None else Decimal(str(actual_usd))
        await conn.execute(
            """
            UPDATE ai_calls
            SET result_state = 'success', actual_usd = $2,
                input_tokens_estimated = COALESCE($3, input_tokens_estimated),
                output_tokens_estimated = COALESCE($4, output_tokens_estimated)
            WHERE id = $1
            """,
            call_id,
            actual,
            input_tokens,
            output_tokens,
        )
        await conn.execute(
            """
            UPDATE daily_usage
            SET reserved_usd = GREATEST(reserved_usd - $1, 0),
                known_usd = known_usd + $2,
                updated_at = now()
            WHERE day = CURRENT_DATE
            """,
            reserved,
            actual,
        )
        from app.services import metrics

        metrics.REGISTRY.inc(metrics.COST_ACTUAL_USD, metrics.micros(actual))
    elif outcome == "known_failure":
        await conn.execute(
            "UPDATE ai_calls SET result_state = 'known_failure', actual_usd = 0 WHERE id = $1",
            call_id,
        )
        await conn.execute(
            """
            UPDATE daily_usage
            SET reserved_usd = GREATEST(reserved_usd - $1, 0), updated_at = now()
            WHERE day = CURRENT_DATE
            """,
            reserved,
        )
    else:  # unknown: резерв не освобождается автоматически
        await conn.execute(
            "UPDATE ai_calls SET result_state = 'unknown' WHERE id = $1", call_id
        )
        from app.services import metrics

        metrics.REGISTRY.inc(metrics.COST_UNKNOWN_USD, metrics.micros(reserved))
        await conn.execute(
            """
            UPDATE daily_usage
            SET reserved_usd = GREATEST(reserved_usd - $1, 0),
                unknown_usd = unknown_usd + $1,
                updated_at = now()
            WHERE day = CURRENT_DATE
            """,
            reserved,
        )
