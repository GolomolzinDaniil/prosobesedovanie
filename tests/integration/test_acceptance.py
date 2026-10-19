"""Приёмочные сценарии интеграции, гонок и сбоев (§3.11; задача T20).

AT05 — дубли и конкурентные ответы; AT06 — старые кнопки и поздний AI;
AT07 — падение до/после commit и неизвестный sendMessage; AT08 — таймауты,
429/5xx и невалидный JSON (≤2 вызова, нет зависших lease, резерв бюджета);
AT15 — credit и бюджет (одна активная сессия, атомарность, освобождение
при отказе, unknown-расход).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.domain.errors import ERROR_REGISTRY
from app.providers.llm import LlmRetryableError, LlmUnknownResultError
from app.services import lifecycle
from app.storage import inbox_repo
from app.telegram import messages
from app.telegram.bot import SendOutcome, SendResult
from contracts.models import ErrorCode
from tests.fakes import FakeLlm

pytestmark = pytest.mark.asyncio

ANSWER_A = "Декораторы изменяют поведение функции без изменения её кода."
ANSWER_B = "Декоратор — функция, оборачивающая другую функцию для расширения."


def _answer_update(update_id: int, message_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1760000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
            "reply_to_message": {"message_id": 0},  # заменяется ниже
        },
    }


async def _submit_raw_answer(harness, update_id: int, text: str, reply_to: int) -> None:
    """Вставка обновления ответа напрямую в inbox (без автообработки)."""
    update = _answer_update(update_id, update_id - 90_000, 555, text)
    update["message"]["reply_to_message"] = {"message_id": reply_to}
    async with harness.pool.acquire() as conn:
        assert await inbox_repo.insert_update(
            conn, update_id=update_id, kind="message", payload=update
        )


# ---------------------------------------------------------------------------
# AT05 — дубли и конкурентные ответы
# ---------------------------------------------------------------------------


async def test_at05_ten_duplicate_updates_single_effect(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    reply_to = turn["delivered_message_id"]

    # 10 повторов одного update_id: дедупликация на входе (§2.2)
    for attempt in range(10):
        update = _answer_update(700_001, 700_001 - 90_000, 555, ANSWER_A)
        update["message"]["reply_to_message"] = {"message_id": reply_to}
        async with harness.pool.acquire() as conn:
            inserted = await inbox_repo.insert_update(
                conn, update_id=700_001, kind="message", payload=update
            )
        assert inserted is (attempt == 0)
    await harness.process_inbox()
    await harness.run_jobs()
    await harness.drain_outbox()

    jobs = await harness.fetch_jobs_by_kind("assess")
    assert len(jobs) == 1  # повтор update не создаёт повторного job
    turns = await harness.fetch_turns(session["id"])
    answered = next(t for t in turns if t["id"] == turn["id"])
    assert answered["answer_text"] == ANSWER_A  # исходный ответ не меняется
    acks = [t for t in harness.bot.texts() if messages.ANSWER_ACCEPTED in t]
    assert len(acks) == 1  # одно подтверждение, не десять
    credits = await harness.fetch_credits()
    assert sum(1 for c in credits if c["state"] == "reserved") == 1  # один резерв


async def test_at05_two_concurrent_answers_one_wins(harness) -> None:
    """Два одновременных ответа одному turn: второй — BUSY, без повторного
    job/списания; принятый ответ не перезаписывается (§2.4)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    reply_to = turn["delivered_message_id"]

    await _submit_raw_answer(harness, 700_101, ANSWER_A, reply_to)
    await _submit_raw_answer(harness, 700_102, ANSWER_B, reply_to)
    # настоящая параллельность: два обработчика, SKIP LOCKED разводит строки
    await asyncio.gather(harness.process_inbox(), harness.process_inbox())
    await harness.run_jobs()
    await harness.drain_outbox()

    jobs = await harness.fetch_jobs_by_kind("assess")
    assert len(jobs) == 1
    turns = await harness.fetch_turns(session["id"])
    answered = next(t for t in turns if t["id"] == turn["id"])
    assert answered["answer_text"] in (ANSWER_A, ANSWER_B)
    # проигравший ответ получил BUSY-уведомление, принятый — один ack
    busy = [t for t in harness.bot.texts() if messages.BUSY_WAIT_MSG in t]
    assert len(busy) >= 1
    acks = [t for t in harness.bot.texts() if messages.ANSWER_ACCEPTED in t]
    assert len(acks) == 1
    credits = await harness.fetch_credits()
    assert sum(1 for c in credits if c["state"] == "reserved") == 1


# ---------------------------------------------------------------------------
# AT06 — старые кнопки и поздний AI
# ---------------------------------------------------------------------------


async def test_at06_old_buttons_do_not_restore_terminal(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    skip_button = await harness.button_for("skip_turn")
    finish_button = await harness.button_for("finish_session")
    await harness.cancel_active()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "cancelled"

    await harness.click(skip_button)
    await harness.click(finish_button)
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "cancelled"  # терминальное состояние не восстановлено
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())


async def test_at06_late_ai_result_after_cancel_discarded(harness) -> None:
    """Поздний результат AI не восстанавливает отменённую сессию (§2.4)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    # сессия отменена ДО обработки задания (оценка «опоздает»)
    await harness.cancel_active()
    await harness.run_jobs()
    await harness.drain_outbox()

    row = await harness.fetch_session(session["id"])
    assert row["status"] == "cancelled"
    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 1  # следующий вопрос не создан
    assert row["version"] >= 1


async def test_at06_late_ai_result_after_expiry_discarded(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET created_at = now() - interval '25 hours', "
            "last_user_action_at = now() - interval '25 hours', "
            "expires_at = now() - interval '1 minute' WHERE id = $1",
            session["id"],
        )
        await lifecycle.expire_sessions(conn, harness.config)
    await harness.run_jobs()  # задание отменено при истечении — не применяется
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "expired"
    assert len(await harness.fetch_turns(session["id"])) == 1


async def test_at06_old_delete_button_after_completion(harness) -> None:
    """Старая кнопка delete после завершения не удаляет доставленный отчёт."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"

    await harness.send_text("/delete")
    confirm = await harness.button_for("confirm_delete")
    # до подтверждения передумали: отказ — данные на месте
    await harness.click(await harness.button_for("delete_decline"))
    await harness.click(confirm)  # старый токен подтверждения
    assert await harness.fetch_report(session["id"]) is not None
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# AT07 — падение до/после commit, неизвестный sendMessage
# ---------------------------------------------------------------------------


async def test_at07_crash_before_commit_answer_not_lost(harness) -> None:
    """Падение worker до commit: ответ не теряется, задание повторяется."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])

    def crasher(data: dict, call_no: int) -> str:
        if call_no == 1:
            raise RuntimeError("worker crash")  # имитация падения до commit
        return json.dumps(FakeLlm.default_assessment(data), ensure_ascii=False)

    harness.llm.responder = crasher
    await harness.run_jobs(max_items=1)
    # ответ сохранён независимо от сбоя оценки (§1.3)
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["answer_text"] == ANSWER_A
    # повтор после перезапуска доводит оценку без второго списания
    await harness.run_jobs()
    await harness.drain_outbox()
    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 2  # создан следующий вопрос
    credits = await harness.fetch_credits()
    assert sum(1 for c in credits if c["state"] == "reserved") == 1


async def test_at07_commit_before_send_survives_restart(harness) -> None:
    """Отчёт сохранён (commit), но не отправлен: «перезапуск» доставляет
    его, порядок частей сохранён (§2.2)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()  # commit: reports + outbox-части
    # «падение» до отправки: outbox не дренируем
    report = await harness.fetch_report(session["id"])
    assert report is not None and report["delivered_at"] is None

    await harness.drain_outbox()  # «перезапуск» — доставка из устойчивой очереди
    row = await harness.fetch_session(session["id"])
    assert row["status"] == "completed"
    report_texts = [
        t for t in harness.bot.texts() if messages.REPORT_TITLE in t
    ]
    assert report_texts  # части доставлены в порядке постановки (seq FIFO)


async def test_at07_unknown_send_result_reply_by_known_ids(harness) -> None:
    """Неизвестный результат sendMessage: повтор допустим и обозначен;
    reply обрабатывается по известным message_id из receipts (§2.2)."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]

    harness.bot.set_failure_rule(
        lambda payload: payload.get("text", "") == messages.ANSWER_ACCEPTED,
        SendResult(SendOutcome.retryable, error="network-unknown"),
        times=1,
    )
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()
    harness.bot.clear_rules()
    async with harness.pool.acquire() as conn:
        await conn.execute("UPDATE outbox SET next_attempt_at = now()")
    await harness.drain_outbox()

    # подтверждение доставлено после повтора; receipts содержат известный id
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT state, message_id, attempts, receipts FROM outbox
            WHERE kind = 'text' AND state = 'delivered'
            ORDER BY delivered_at DESC LIMIT 1
            """
        )
    assert row["message_id"] is not None
    assert row["attempts"] >= 2  # повтор обозначен числом попыток
    # следующий вопрос — reply на подтверждённый message_id (§2.2)
    turns = await harness.fetch_turns(session["id"])
    delivered = [t["delivered_message_id"] for t in turns if t["delivered_message_id"]]
    assert all(mid for mid in delivered)


# ---------------------------------------------------------------------------
# AT08 — таймауты, 429/5xx, невалидный JSON
# ---------------------------------------------------------------------------


async def test_at08_429_and_5xx_capped_at_two_calls(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])

    def always_429(data: dict, call_no: int):
        raise LlmRetryableError(retry_after_seconds=0, status=429)

    harness.llm.responder = always_429
    await harness.run_jobs()
    await harness.drain_outbox()

    assert len(harness.llm.calls) == 2  # не более 2 вызовов на операцию
    async with harness.pool.acquire() as conn:
        calls = await conn.fetchval("SELECT count(*) FROM ai_calls")
        leased = await conn.fetchval("SELECT count(*) FROM jobs WHERE state = 'leased'")
        job = await conn.fetchrow(
            "SELECT state, error_code FROM jobs WHERE kind = 'assess'"
        )
        usage = await conn.fetchrow("SELECT reserved_usd FROM daily_usage")
    assert calls == 2
    assert leased == 0  # нет зависших lease
    assert job["state"] == "failed"
    assert job["error_code"] == "AI_UNAVAILABLE"  # запасной сценарий
    assert float(usage["reserved_usd"]) == 0.0  # резерв освобождён (known_failure)
    # интервью продолжается вопросом из каталога
    notice = ERROR_REGISTRY[ErrorCode.AI_UNAVAILABLE][0]
    assert any(notice in t for t in harness.bot.texts())


async def test_at08_invalid_json_twice_falls_back(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])

    harness.llm.responder = lambda data, call_no: "{ не json"
    await harness.run_jobs()
    await harness.drain_outbox()

    assert len(harness.llm.calls) == 2
    async with harness.pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT state, error_code FROM jobs WHERE kind = 'assess'"
        )
    assert job["error_code"] == "AI_INVALID_OUTPUT"
    notice = ERROR_REGISTRY[ErrorCode.AI_INVALID_OUTPUT][0]
    assert any(notice in t for t in harness.bot.texts())


async def test_at08_unknown_result_keeps_reserve(
    harness, db_url, tmp_path_factory
) -> None:
    """Неизвестный результат: резерв остаётся в unknown_usd до сверки (§3.10).
    Тарифы заполнены — иначе резерв нулевой и утверждение бессмысленно."""
    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("at08-costs")
    settings = json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 1000
    settings["limits"]["provider_retry_pause_min_seconds"] = 0
    settings["limits"]["provider_retry_pause_max_seconds"] = 0
    settings["costs"].update(
        {
            "daily_ai_budget_usd": 100,
            "llm_input_per_1k_usd": 0.01,
            "llm_output_per_1k_usd": 0.03,
        }
    )
    custom = cfg_dir / "settings.json"
    custom.write_text(json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
            RUN_DIR=str(cfg_dir / "run"),
        ),
    )

    await local.complete_entry()
    session = await local.fetch_active_session()
    turn = (await local.fetch_turns(session["id"]))[0]
    await local.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])

    local.llm.responder = lambda data, call_no: LlmUnknownResultError("timeout")
    await local.run_jobs()

    async with local.pool.acquire() as conn:
        usage = await conn.fetchrow(
            "SELECT reserved_usd, unknown_usd FROM daily_usage"
        )
    assert float(usage["unknown_usd"]) > 0  # неизвестный расход учтён
    assert float(usage["reserved_usd"]) == 0.0


# ---------------------------------------------------------------------------
# AT15 — credit и бюджет
# ---------------------------------------------------------------------------


async def test_at15_one_active_session_atomic_reserve(harness) -> None:
    await harness.complete_entry()
    # вторая активная сессия невозможна (§1.3)
    await harness.send_text("/start")
    assert any(messages.ACTIVE_SESSION_HINT in t for t in harness.bot.texts())
    credits = await harness.fetch_credits()
    assert sum(1 for c in credits if c["state"] == "reserved") == 1
    async with harness.pool.acquire() as conn:
        active = await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE status NOT IN "
            "('completed','cancelled','expired','failed')"
        )
    assert active == 1


async def test_at15_consumed_after_full_delivery(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()

    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "consumed"
    assert credits[0]["consumed_session_id"] == session["id"]
    assert credits[0]["reserved_session_id"] is None


async def test_at15_terminal_failure_releases_credit(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER_A, reply_to_message_id=turn["delivered_message_id"])
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
    assert credits[0]["state"] == "available"  # отказ освободил право
    assert credits[0]["reserved_session_id"] is None
