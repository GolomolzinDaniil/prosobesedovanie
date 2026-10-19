"""Нагрузка и лимиты параллельности (§1.3, §3.10; задача T22, AT14).

Расчётная нагрузка: 20 активных сессий; не более 5 параллельных LLM, 2 STT
и 2 TTS — остальные задания в очереди. Проверяется фактическая работа
семафоров jobs-worker при конкурентной обработке и отсутствие зависших
заданий после насыщения. Полный протокол 20 сессий с p95 по этапам формирует
scripts/load_test.py (планирование — planning/load-test-protocol.md).
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.asyncio

ANSWER = "Декораторы изменяют поведение функции; использую functools.wraps."


def _track(obj, method_name: str, delay: float) -> dict:
    """Обёртка метода фейк-провайдера: счётчик активной параллельности."""
    state = {"active": 0, "peak": 0, "calls": 0}
    original = getattr(obj, method_name)

    async def tracked(*args, **kwargs):
        state["active"] += 1
        state["calls"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(delay)
            return await original(*args, **kwargs)
        finally:
            state["active"] -= 1

    setattr(obj, method_name, tracked)
    return state


async def _run_worker_until_drained(harness, worker, seconds: float = 25.0) -> None:
    task = asyncio.create_task(worker.run())
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            async with harness.pool.acquire() as conn:
                pending = await conn.fetchval(
                    "SELECT count(*) FROM jobs WHERE state IN ('pending', 'leased')"
                )
            if pending == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=10)


async def test_llm_concurrency_capped_at_five(harness) -> None:
    """12 конкурентных оценок: пик параллельных LLM-вызовов ≤ 5 (§1.3)."""
    n_users = 12
    llm_state = _track(harness.llm, "complete", delay=0.15)

    for i in range(n_users):
        await harness.complete_entry(chat_id=1000 + i)
    for i in range(n_users):
        chat = 1000 + i
        session = await harness.fetch_active_session(chat)
        turn = (await harness.fetch_turns(session["id"]))[0]
        await harness.send_text(
            ANSWER, chat_id=chat, reply_to_message_id=turn["delivered_message_id"]
        )

    async with harness.pool.acquire() as conn:
        queued = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE kind = 'assess' AND state = 'pending'"
        )
    assert queued == n_users  # все задания в очереди до старта worker

    await _run_worker_until_drained(harness, harness.jobs)

    assert llm_state["calls"] == n_users
    assert llm_state["peak"] <= 5
    assert llm_state["peak"] >= 2  # параллелизм фактически использован
    async with harness.pool.acquire() as conn:
        stuck = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE state IN ('pending', 'leased')"
        )
        failed = await conn.fetchval("SELECT count(*) FROM jobs WHERE state = 'failed'")
    assert stuck == 0 and failed == 0  # нет зависших lease и сбоев


async def test_stt_and_tts_concurrency_capped_at_two(harness) -> None:
    """6 голосовых сессий: пик STT ≤ 2 и TTS ≤ 2 (§1.3)."""
    n_users = 6
    stt_state = _track(harness.stt, "transcribe", delay=0.15)
    tts_state = _track(harness.tts, "synthesize", delay=0.15)

    for i in range(n_users):
        await harness.complete_entry(chat_id=2000 + i, fmt="voice")
    for i in range(n_users):
        chat = 2000 + i
        session = await harness.fetch_active_session(chat)
        turn = (await harness.fetch_turns(session["id"]))[0]
        await harness.send_voice(
            chat_id=chat,
            reply_to=turn["delivered_message_id"],
            duration=8,
            file_id=f"load-voice-{i}",
        )

    await _run_worker_until_drained(harness, harness.jobs, seconds=30.0)

    assert stt_state["calls"] == n_users
    assert stt_state["peak"] <= 2
    assert stt_state["peak"] >= 2 or n_users < 2
    assert tts_state["calls"] >= 1  # озвучка первых вопросов
    assert tts_state["peak"] <= 2
    async with harness.pool.acquire() as conn:
        stuck = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE state IN ('pending', 'leased')"
        )
    assert stuck == 0


async def test_queue_overload_falls_back_after_budget(harness) -> None:
    """Очередь AI > 60 с: задание уходит в запасной сценарий без вызова
    провайдера (§1.3); права пользователя сохранены."""
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET created_at = now() - interval '2 minutes' "
            "WHERE kind = 'assess'"
        )
    calls_before = len(harness.llm.calls)
    await harness.run_jobs()
    await harness.drain_outbox()
    assert len(harness.llm.calls) == calls_before  # вызова не было
    row = await harness.fetch_session(session["id"])
    assert row["status"] in ("awaiting_answer", "delivering_question", "evaluating")
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "reserved"  # право сохранено
