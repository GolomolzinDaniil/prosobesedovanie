"""Персональный отчёт (§2.2, §3.4, §3.8; задачи T13, T14).

Генерация FeedbackReport 2.0, валидация доказательств, детерминированный
запасной отчёт (degraded), разбиение на части, последовательная доставка,
списание попытки после подтверждения всех частей, /report без нового
LLM-вызова, отмена после начала доставки, отказ доставки с освобождением
резерва.
"""

from __future__ import annotations

import json

import pytest

from app.telegram import messages
from app.telegram.bot import SendOutcome, SendResult
from tests.fakes import FakeLlm

pytestmark = pytest.mark.asyncio

ANSWERS = [
    "Учусь на третьем курсе, пишу учебный проект по анализу данных.",
    "Проект: собирал данные опросов студентов и считал статистику в Python.",
    "Коллекции: list изменяемый, tuple нет, dict и set дают O(1) для поиска.",
    "Исключения: try/except/else/finally, перехватываю конкретные классы.",
    "Области видимости: LEGB, nonlocal для замыканий, global для модуля.",
    "Данные: читаю построчно через with open, фильтрую генераторным выражением.",
]


async def last_session(harness):
    async with harness.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1"
        )


async def evaluate(harness) -> None:
    await harness.run_jobs()
    await harness.drain_outbox()


async def full_interview(harness, answers: list[str] | None = None) -> None:
    """Полный план: 6 ответов -> отчёт доставлен, сессия завершена."""
    await harness.complete_entry()
    for answer in answers or ANSWERS:
        session = await harness.fetch_active_session()
        turn = next(
            t
            for t in await harness.fetch_turns(session["id"])
            if t["id"] == session["current_turn_id"]
        )
        await harness.send_text(answer, reply_to_message_id=turn["delivered_message_id"])
        await evaluate(harness)


async def early_finish_interview(harness) -> None:
    """Один ответ и «Завершить» -> partial-отчёт."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWERS[0], reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await evaluate(harness)


def report_responder(make_report_output):
    """Responder, подменяющий только ответ для операции report."""

    def responder(data: dict, call_no: int) -> str:
        if "snapshot_turns" in data:
            return make_report_output(data, call_no)
        return json.dumps(FakeLlm.default_assessment(data), ensure_ascii=False)

    return responder


# ---------------------------------------------------------------------------
# Доставка, списание попытки, финальные кнопки (§2.2, §3.7, §3.8)
# ---------------------------------------------------------------------------


async def test_report_delivery_completes_session(harness) -> None:
    await full_interview(harness)
    session = await last_session(harness)
    assert session["status"] == "completed"
    report = await harness.fetch_report(session["id"])
    assert report is not None
    assert report["partial"] is False
    assert report["degraded"] is False
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    assert 1 <= len(parts) <= 4
    assert all(len(part) <= 3500 for part in parts)
    assert report["total_chars"] <= 12000
    # delivery_started_at — до первой попытки; delivered_at — после всех частей
    assert report["delivery_started_at"] is not None
    assert report["delivered_at"] is not None
    # части доставлены дословно (без исполнения Markdown/HTML)
    texts = harness.bot.texts()
    for part in parts:
        assert part in texts
    # порядок разделов внутри полного текста (§3.4)
    full = "\n\n".join(parts)
    positions = [
        full.index("Параметры:"),
        full.index("Разобранные темы:"),
        full.index("Сильные стороны:"),
        full.index("Ошибки и пробелы:"),
        full.index("Приоритетные действия:"),
    ]
    assert positions == sorted(positions)
    # попытка списана после подтверждения всех частей (§3.8)
    credits = await harness.fetch_credits()
    assert len(credits) == 1
    assert credits[0]["state"] == "consumed"
    assert credits[0]["consumed_session_id"] == session["id"]
    # события
    async with harness.pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT name FROM events WHERE session_id = $1", session["id"]
        )
    names = {e["name"] for e in events}
    assert {"report_delivered", "trial_consumed"} <= names
    # финальные кнопки: оценка полезности и «Новая тренировка»
    assert any(messages.REPORT_RATING_PROMPT in t for t in texts)
    await harness.button_for("report_rating_useful")
    await harness.button_for("report_rating_not_useful")
    await harness.button_for("new_training")


async def test_report_rating_is_single_event(harness) -> None:
    await early_finish_interview(harness)
    session = await last_session(harness)
    await harness.click(await harness.button_for("report_rating_useful"))
    assert any(messages.REPORT_RATING_THANKS in t for t in harness.bot.texts())
    await harness.click(await harness.button_for("report_rating_not_useful"))
    async with harness.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT props_category FROM events WHERE name = 'report_rating' "
            "AND session_id = $1",
            session["id"],
        )
    # оценка необязательна и принимается один раз (дедупликация по сессии)
    assert len(rows) == 1
    props = rows[0]["props_category"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["rating"] == "useful"


async def test_new_training_restarts_entry(harness) -> None:
    await early_finish_interview(harness)
    await harness.click(await harness.button_for("new_training"))
    assert any(messages.NEW_TRAINING_STARTED_HINT in t for t in harness.bot.texts())
    # экран выбора роли снова активен; попытка уже списана — новой нет
    await harness.button_for("select_role", "python")
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "consumed"


# ---------------------------------------------------------------------------
# Валидация модели и запасной отчёт (§3.4)
# ---------------------------------------------------------------------------


async def test_invalid_report_json_degrades(harness) -> None:
    harness.llm.responder = report_responder(lambda data, n: "это не JSON")
    await early_finish_interview(harness)
    session = await last_session(harness)
    report = await harness.fetch_report(session["id"])
    assert report["degraded"] is True
    # предел два вызова на операцию (§3.2)
    report_calls = [c for c in harness.llm.calls if c.operation == "report"]
    assert len(report_calls) == 2
    assert any(
        messages.REPORT_DEGRADED_LABEL in t for t in harness.bot.texts()
    )
    assert session["status"] == "completed"


async def test_extra_text_around_json_degrades(harness) -> None:
    """Лишний текст вокруг JSON запрещён — отчёт уходит в запасной (§3.4)."""

    def make(data, call_no):
        return "Отчёт:\n" + json.dumps(FakeLlm.default_report(data), ensure_ascii=False)

    harness.llm.responder = report_responder(make)
    await early_finish_interview(harness)
    session = await last_session(harness)
    report = await harness.fetch_report(session["id"])
    assert report["degraded"] is True


async def test_fabricated_quote_degrades(harness) -> None:
    """Цитата, не найденная в ответе дословно, отклоняет весь отчёт (§3.4)."""

    def make(data, call_no):
        rep = FakeLlm.default_report(data)
        if rep["strengths"]:
            rep["strengths"][0]["quote"] = "выдуманная цитата"
        elif rep["gaps"] and rep["gaps"][0]["quote"] is not None:
            rep["gaps"][0]["quote"] = "выдуманная цитата"
        return json.dumps(rep, ensure_ascii=False)

    harness.llm.responder = report_responder(make)
    await early_finish_interview(harness)
    session = await last_session(harness)
    report = await harness.fetch_report(session["id"])
    assert report["degraded"] is True
    # выдуманная цитата не попала в доставленный текст
    assert all("выдуманная цитата" not in t for t in harness.bot.texts())
    stored = report["report_json"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert "выдуманная цитата" not in json.dumps(stored, ensure_ascii=False)


async def test_partial_label_on_early_finish(harness) -> None:
    await early_finish_interview(harness)
    session = await last_session(harness)
    report = await harness.fetch_report(session["id"])
    assert report["partial"] is True
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    assert any(messages.REPORT_PARTIAL_LABEL in part for part in parts)
    stored = report["report_json"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored["partial"] is True
    assert stored["coverage"]["main_total"] == 1
    assert stored["coverage"]["main_answered"] == 1


async def test_token_overflow_uses_deterministic_report(harness) -> None:
    """Ответы целиком не помещаются в токен-лимит: без обрезки — запасной
    детерминированный отчёт, LLM для отчёта не вызывается (§3.4)."""
    long_answers = [
        f"{base} " + "подробности проекта и технические детали ответа. " * 78
        for base in ANSWERS
    ]
    assert all(len(a) <= 4000 for a in long_answers)
    await full_interview(harness, answers=long_answers)
    session = await last_session(harness)
    report = await harness.fetch_report(session["id"])
    assert report["degraded"] is True
    assert all(c.operation == "assess" for c in harness.llm.calls)
    assert session["status"] == "completed"
    assert any(messages.REPORT_DEGRADED_LABEL in t for t in harness.bot.texts())


# ---------------------------------------------------------------------------
# Повторная выдача, отмена и отказ доставки (§2.2, §2.4, §3.8)
# ---------------------------------------------------------------------------


async def test_report_command_redelivers_without_new_attempt(harness) -> None:
    await early_finish_interview(harness)
    calls_before = len(harness.llm.calls)
    starts_before = sum(
        1 for t in harness.bot.texts() if t.startswith(messages.REPORT_TITLE)
    )
    assert starts_before == 1

    await harness.send_text("/report")

    starts_after = sum(
        1 for t in harness.bot.texts() if t.startswith(messages.REPORT_TITLE)
    )
    assert starts_after == starts_before + 1  # части выданы повторно
    assert len(harness.llm.calls) == calls_before  # без нового LLM-вызова
    async with harness.pool.acquire() as conn:
        reports = await conn.fetchval("SELECT count(*) FROM reports")
    assert reports == 1  # сохранённый отчёт воспроизводится, не генерируется
    credits = await harness.fetch_credits()
    assert len(credits) == 1 and credits[0]["state"] == "consumed"


async def test_cancel_after_delivery_started_is_busy(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWERS[0], reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()  # отчёт сохранён, части в очереди, доставка не начата

    # Первая попытка доставки неуспешна (повтор через backoff): worker уже
    # поставил delivery_started_at, сессия остаётся reporting (§2.4).
    harness.bot.set_failure_rule(
        lambda payload: str(payload.get("text", "")).startswith(messages.REPORT_TITLE),
        SendResult(SendOutcome.retryable, error="scripted"),
    )
    await harness.drain_outbox()
    report = await harness.fetch_report(session["id"])
    assert report["delivery_started_at"] is not None

    # отмена выполняется только кнопками cancel_session -> cancel_confirm (§3.7)
    await harness.cancel_active()
    assert any(messages.CANCEL_ALREADY_SENDING in t for t in harness.bot.texts())
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "reporting"  # отмена не прошла

    harness.bot.clear_rules()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE outbox SET next_attempt_at = now() WHERE state = 'pending'"
        )
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"


async def test_report_delivery_failure_releases_credit(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWERS[0], reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()

    harness.bot.set_failure_rule(
        lambda payload: str(payload.get("text", "")).startswith(messages.REPORT_TITLE),
        SendResult(SendOutcome.bad_request, error="scripted"),
    )
    await harness.drain_outbox()

    row = await harness.fetch_session(session["id"])
    assert row["status"] == "failed"
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "available"  # резерв освобождён
    assert credits[0]["reserved_session_id"] is None
    assert any(
        messages.QUESTION_DELIVERY_FAILED in t for t in harness.bot.texts()
    )
    # остальные части отменены, не висят в очереди
    async with harness.pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE session_id = $1 "
            "AND kind = 'report_part' AND state = 'pending'",
            session["id"],
        )
    assert pending == 0


async def test_report_saved_before_sending_reproducible(harness) -> None:
    """Отчёт сохраняется до отправки и воспроизводится без LLM (§2.2)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWERS[0], reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()

    report = await harness.fetch_report(session["id"])
    assert report is not None  # сохранён до доставки
    parts = report["parts"]
    if isinstance(parts, str):
        parts = json.loads(parts)
    assert report["delivery_started_at"] is None  # отправка ещё не начиналась

    # LLM больше недоступен — доставка сохранённого отчёта не требует модели
    harness.deps.llm = None
    harness.llm.responder = None
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"
    for part in parts:
        assert part in harness.bot.texts()


async def test_finish_from_awaiting_marks_current_skipped(harness) -> None:
    """«Завершить» из awaiting_answer: текущий неотвеченный вопрос считается
    пропущенным, отчёт строится без сбоя задания (§3.3, §3.4)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWERS[0], reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()  # оценка принята, создан main2 (без ответа)
    await harness.drain_outbox()
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()

    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"
    report = await harness.fetch_report(session["id"])
    assert report is not None
    assert report["partial"] is True
    async with harness.pool.acquire() as conn:
        skipped = await conn.fetchval(
            "SELECT count(*) FROM turns WHERE session_id = $1 AND skipped",
            session["id"],
        )
        stuck = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE kind = 'report' AND state <> 'done'"
        )
    assert skipped == 1  # текущий вопрос помечен пропущенным
    assert stuck == 0    # report-задание не зависло
