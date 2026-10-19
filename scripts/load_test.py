"""Нагрузочная проверка: 20 активных сессий (§1.3, §3.11 AT14; задача T22).

Прогоняет расчётную нагрузку через полный worker-конвейер (inbox -> jobs ->
outbox) против реального PostgreSQL со стаб-провайдерами и измеряет задержки
этапов по определениям §1.3 — от приёма обновления до подтверждённой отправки
соответствующего сообщения (по меткам времени БД):

  first_text_question  <= 5 c    клик «Начать» -> доставка первого вопроса
  action_ack           <= 2 c    ответ -> подтверждение принятия
  next_text_question   <= 15 c   ответ -> доставка следующего вопроса
  text_report          <= 45 c   клик «Завершить» -> последняя часть отчёта
  transcription        <= 45 c   голосовое сообщение -> экран подтверждения
  voiced_question      <= 60 c   подтверждение расшифровки -> озвученный вопрос

Это проверка ёмкости конвейера, а не гарантия внешних API: протокол на
выбранных провайдерах и реальных Telegram-клиентах фиксируется отдельно
(planning/load-test-protocol.md, условие запуска T23).

Запуск (только против тестовой БД — схема очищается):
    TEST_DATABASE_URL=postgresql://.../prosob_test \
        python scripts/load_test.py [--sessions 20] [--voice 4] [--json out.json]

Код возврата: 0 — все p95 в целях §1.3; 1 — превышение; 2 — ошибка окружения.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ANSWERS = [
    "Декораторы изменяют поведение функции; использую functools.wraps для метаданных.",
    "Исключения: try/except/else/finally, перехватываю конкретные классы ошибок.",
]

TABLES = (
    "ai_calls, credits, daily_usage, deletion_tombstones, events, inbox, jobs, "
    "outbox, payments, reports, sessions, turns, ui_tokens, users"
)


def _p(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 3)


async def _user_button(harness, action: str, session_id, allowed_value=None) -> str:
    """callback_data кнопки действия, принадлежащей конкретной сессии."""
    for payload in reversed(harness.bot.sent):
        markup = payload.get("reply_markup") or {}
        for row in markup.get("inline_keyboard") or []:
            for button in row:
                data = button.get("callback_data") or ""
                if not data.startswith(f"{action}:"):
                    continue
                token = data.split(":", 1)[1]
                async with harness.pool.acquire() as conn:
                    rec = await conn.fetchrow(
                        "SELECT action, allowed_value, session_id, consumed_at "
                        "FROM ui_tokens WHERE token = $1",
                        token,
                    )
                if (
                    rec is not None
                    and rec["action"] == action
                    and rec["consumed_at"] is None
                    and rec["session_id"] == session_id
                    and (allowed_value is None or rec["allowed_value"] == allowed_value)
                ):
                    return data
    raise AssertionError(f"кнопка {action} для сессии {session_id} не найдена")


async def _measure(pool, session_ids: list) -> dict[str, list[float]]:
    """Задержки этапов по меткам времени БД (received_at -> delivered_at)."""
    out: dict[str, list[float]] = {}
    async with pool.acquire() as conn:
        # ответ -> подтверждение принятия (ack:{turn})
        rows = await conn.fetch(
            """
            SELECT (ob.delivered_at - i.received_at) AS d
            FROM inbox i
            JOIN turns t ON t.delivered_message_id =
                ((i.payload->'message'->'reply_to_message')->>'message_id')::bigint
            JOIN outbox ob ON ob.unique_key = 'ack:' || t.id
                AND ob.state = 'delivered'
            WHERE i.kind = 'message' AND i.status = 'processed'
              AND t.session_id = ANY($1::uuid[])
            """,
            session_ids,
        )
        out["action_ack"] = [r["d"].total_seconds() for r in rows]

        # ответ -> доставка следующего вопроса (ordinal + 1)
        rows = await conn.fetch(
            """
            SELECT (ob.delivered_at - i.received_at) AS d
            FROM inbox i
            JOIN turns answered ON answered.delivered_message_id =
                ((i.payload->'message'->'reply_to_message')->>'message_id')::bigint
            JOIN turns nxt ON nxt.session_id = answered.session_id
                AND nxt.ordinal = answered.ordinal + 1
            JOIN outbox ob ON ob.unique_key = 'question:' || nxt.id
                AND ob.state = 'delivered'
            WHERE i.kind = 'message' AND i.status = 'processed'
              AND answered.session_id = ANY($1::uuid[])
            """,
            session_ids,
        )
        out["next_text_question"] = [r["d"].total_seconds() for r in rows]

        # клик «Начать» -> доставка первого вопроса
        rows = await conn.fetch(
            """
            SELECT (ob.delivered_at - i.received_at) AS d
            FROM inbox i
            JOIN sessions s ON s.user_id = i.user_id
                AND s.id = ANY($1::uuid[])
            JOIN turns first_t ON first_t.session_id = s.id AND first_t.ordinal = 1
            JOIN outbox ob ON ob.unique_key = 'question:' || first_t.id
                AND ob.state = 'delivered'
            WHERE i.kind = 'callback_query' AND i.status = 'processed'
              AND i.payload->'callback_query'->>'data' LIKE 'start_session:%'
            """,
            session_ids,
        )
        out["first_text_question"] = [r["d"].total_seconds() for r in rows]

        # клик «Завершить» -> последняя часть отчёта
        rows = await conn.fetch(
            """
            SELECT (fin.last_part - i.received_at) AS d
            FROM inbox i
            JOIN LATERAL (
                SELECT max(ob.delivered_at) AS last_part
                FROM outbox ob
                WHERE ob.kind = 'report_part' AND ob.state = 'delivered'
                  AND ob.user_id = i.user_id
            ) fin ON fin.last_part IS NOT NULL
            WHERE i.kind = 'callback_query' AND i.status = 'processed'
              AND i.payload->'callback_query'->>'data' LIKE 'finish_session:%'
              AND i.user_id IN (SELECT user_id FROM sessions
                                WHERE id = ANY($1::uuid[]))
            """,
            session_ids,
        )
        out["text_report"] = [r["d"].total_seconds() for r in rows]

        # голосовое сообщение -> экран подтверждения расшифровки
        rows = await conn.fetch(
            """
            SELECT (ob.delivered_at - i.received_at) AS d
            FROM inbox i
            JOIN users vu ON vu.telegram_user_id =
                (i.payload->'message'->'from'->>'id')::bigint
            JOIN outbox ob ON ob.unique_key LIKE 'transcript-buttons:%'
                AND ob.state = 'delivered'
                AND ob.user_id = vu.id
                AND ob.created_at >= i.received_at
            WHERE i.kind = 'message' AND i.status = 'processed'
              AND i.payload->'message' ? 'voice'
              AND i.user_id IN (SELECT user_id FROM sessions
                                WHERE id = ANY($1::uuid[]))
            """,
            session_ids,
        )
        out["transcription"] = [r["d"].total_seconds() for r in rows]

        # подтверждение расшифровки -> озвученный следующий вопрос
        rows = await conn.fetch(
            """
            SELECT (ob.delivered_at - i.received_at) AS d
            FROM inbox i
            JOIN outbox ob ON ob.kind = 'question_voice' AND ob.state = 'delivered'
                AND ob.user_id = i.user_id AND ob.created_at >= i.received_at
            WHERE i.kind = 'callback_query' AND i.status = 'processed'
              AND i.payload->'callback_query'->>'data' LIKE 'confirm_transcript:%'
              AND i.user_id IN (SELECT user_id FROM sessions
                                WHERE id = ANY($1::uuid[]))
            """,
            session_ids,
        )
        out["voiced_question"] = [r["d"].total_seconds() for r in rows]
    return out


async def _run_jobs_until_drained(harness, seconds: float = 180.0) -> None:
    from app.workers.jobs_worker import JobsWorker

    worker = JobsWorker(harness.pool, harness.deps)  # свежий экземпляр на раунд
    task = asyncio.create_task(worker.run())
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            async with harness.pool.acquire() as conn:
                pending = await conn.fetchval(
                    "SELECT count(*) FROM jobs WHERE state IN ('pending','leased')"
                )
            if pending == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=15)
    await harness.drain_outbox(max_items=1000)


async def _main(sessions_n: int, voice_n: int) -> tuple[dict, dict, int]:
    import asyncpg

    from tests.conftest import (
        TEST_DATABASE_URL,
        make_env,
        write_filled_provider_profile,
    )
    from tests.fakes import Harness

    if "test" not in TEST_DATABASE_URL.lower():
        raise RuntimeError("load_test допускается только на тестовой БД")

    from app.storage.migrate import run_migrations

    await run_migrations(TEST_DATABASE_URL)
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")

    tmp = Path(tempfile.mkdtemp(prefix="loadtest-"))
    settings = json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 100000
    (tmp / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = make_env(
        DATABASE_URL=TEST_DATABASE_URL,
        SETTINGS_PATH=str(tmp / "settings.json"),
        PROVIDER_PROFILE_PATH=write_filled_provider_profile(tmp),
        LLM_API_KEY="load", STT_API_KEY="load", TTS_API_KEY="load",
        MEDIA_DIR=str(tmp / "media"),
        RUN_DIR=str(tmp / "run"),
    )
    harness = Harness(pool, env)
    started = time.monotonic()

    session_ids = []
    users: list[tuple[int, str]] = []  # (chat_id, format)
    for i in range(sessions_n):
        chat = 5_000_000 + i
        fmt = "voice" if i < voice_n else "text"
        users.append((chat, fmt))
        await harness.complete_entry(chat_id=chat, fmt=fmt)
        session = await harness.fetch_active_session(chat)
        session_ids.append(session["id"])

    # Раунд 1: ответы (задания оценки/расшифровки копятся в очереди)
    for chat, fmt in users:
        session = await harness.fetch_active_session(chat)
        turn = next(
            t for t in await harness.fetch_turns(session["id"])
            if t["id"] == session["current_turn_id"]
        )
        if fmt == "voice":
            await harness.send_voice(
                chat_id=chat, reply_to=turn["delivered_message_id"],
                duration=8, file_id=f"load-{chat}",
            )
        else:
            await harness.send_text(
                ANSWERS[0], chat_id=chat,
                reply_to_message_id=turn["delivered_message_id"],
            )
    await _run_jobs_until_drained(harness)

    # Раунд 2: голос подтверждает расшифровку; текст отвечает на второй вопрос
    for chat, _fmt in users:
        session = await harness.fetch_active_session(chat)
        if session is None:
            continue
        if session["status"] == "confirming_transcript":
            turn = next(
                t for t in await harness.fetch_turns(session["id"])
                if t["id"] == session["current_turn_id"]
            )
            await harness.click(
                await _user_button(
                    harness, "confirm_transcript", session["id"],
                    f"{turn['id']}:{turn['transcript_version']}",
                ),
                chat_id=chat,
            )
        elif session["status"] == "awaiting_answer":
            turn = next(
                t for t in await harness.fetch_turns(session["id"])
                if t["id"] == session["current_turn_id"]
            )
            await harness.send_text(
                ANSWERS[1], chat_id=chat,
                reply_to_message_id=turn["delivered_message_id"],
            )
    await _run_jobs_until_drained(harness)

    # Раунд 3: завершение -> отчёт
    for chat, _fmt in users:
        session = await harness.fetch_active_session(chat)
        if session is None:
            continue
        await harness.click(
            await _user_button(harness, "finish_session", session["id"]),
            chat_id=chat,
        )
    await _run_jobs_until_drained(harness)

    measured = await _measure(pool, session_ids)

    async with pool.acquire() as conn:
        completed = await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE id = ANY($1::uuid[]) "
            "AND status = 'completed'",
            session_ids,
        )
        stuck = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE state IN ('pending','leased')"
        )
    await pool.close()

    slo = harness.config.settings.slo_p95_seconds
    stages: dict[str, dict] = {}
    failures = 0
    for stage in (
        "first_text_question", "action_ack", "next_text_question",
        "text_report", "transcription", "voiced_question",
    ):
        values = measured.get(stage) or []
        p95 = _p(values, 95)
        target = getattr(slo, stage, None)
        ok = None if (p95 is None or target is None) else p95 <= target
        if ok is False:
            failures += 1
        stages[stage] = {
            "samples": len(values),
            "p50": _p(values, 50),
            "p95": p95,
            "target_seconds": target,
            "within_slo": ok,
        }
    meta = {
        "sessions": sessions_n,
        "voice_sessions": voice_n,
        "completed": completed,
        "stuck_jobs": stuck,
        "wall_seconds": round(time.monotonic() - started, 1),
    }
    if completed != sessions_n or stuck:
        failures += 1
    return stages, meta, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Нагрузочная проверка §1.3/AT14")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--voice", type=int, default=4)
    parser.add_argument("--json", type=str, default=None)
    args = parser.parse_args()
    db_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://app:app_dev_password@127.0.0.1:5432/prosob_test",
    )
    if "test" not in db_url.lower():
        print("load_test: TEST_DATABASE_URL обязан указывать тестовую БД",
              file=sys.stderr)
        return 2
    try:
        stages, meta, failures = asyncio.run(_main(args.sessions, args.voice))
    except Exception as exc:
        print(f"load_test: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report = {"meta": meta, "stages": stages}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
