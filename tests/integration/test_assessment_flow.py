"""Адаптивное интервью (§2.2, §3.2, §3.3; AT02-частично, AT04-механика,
AT05/AT08-частично): оценка ответа, фиксированное правило действия, шаг
сложности, уточнения, вопросы модели и каталога, запасные сценарии,
бюджет, дедупликация вызовов, поздние результаты.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.llm import LlmConfigError, LlmRetryableError, LlmUnknownResultError
from app.telegram import messages
from tests.fakes import FakeLlm


def scripted(*overrides: Any):
    """Респондер FakeLlm по номеру вызова: dict-параметры оценки либо callable."""

    calls = list(overrides)

    def responder(data: dict, call_no: int):
        item = calls[call_no - 1] if call_no - 1 < len(calls) else {}
        if callable(item):
            return item(data, call_no)
        if isinstance(item, Exception):
            return item  # FakeLlm бросит её из complete()
        if isinstance(item, str):
            return item  # сырой контент модели (напр. невалидный JSON)
        return json.dumps(
            FakeLlm.default_assessment(data, **item), ensure_ascii=False
        )

    return responder


async def answer_current(harness, text: str, chat_id: int = 555):
    session = await harness.fetch_active_session(chat_id)
    assert session is not None, "нет активной сессии"
    turns = await harness.fetch_turns(session["id"])
    turn = next(t for t in turns if t["id"] == session["current_turn_id"])
    await harness.send_text(text, chat_id=chat_id, reply_to_message_id=turn["delivered_message_id"])
    return session, turn


async def evaluate(harness):
    await harness.run_jobs()
    await harness.drain_outbox()


async def start_answered(harness, *, answer="Учусь на третьем курсе, пишу учебный проект.",
                         chat_id=555, level="junior"):
    await harness.complete_entry(chat_id=chat_id, level=level)
    return await answer_current(harness, answer, chat_id)


# ---------------------------------------------------------------------------
# Базовый поток оценки
# ---------------------------------------------------------------------------


async def test_assessment_applied_and_next_question_delivered(harness) -> None:
    session, turn = await start_answered(harness)
    await evaluate(harness)

    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 2
    first = next(t for t in turns if t["id"] == turn["id"])
    assert first["assessment"] is not None
    assessment = json.loads(first["assessment"])
    assert assessment["scores"]["evidence"] == 3  # HR-вопрос: evidence, не correctness
    assert assessment["scores"]["correctness"] is None
    # цитата буквально из ответа
    assert first["answer_text"].startswith(assessment["evidence_quotes"][0].split(",")[0][:20])

    updated = await harness.fetch_active_session()
    assert updated["status"] == "awaiting_answer"
    second = next(t for t in turns if t["ordinal"] == 2)
    assert second["main_index"] == 2 and second["topic"] == "project_story"
    assert second["delivered_message_id"] is not None

    # задание done, вызов провайдера учтён
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job["state"] == "done"
    assert json.loads(job["result"])["decision"] == "next_main"
    async with harness.pool.acquire() as conn:
        calls = await conn.fetch("SELECT * FROM ai_calls")
        usage = await conn.fetchrow("SELECT * FROM daily_usage")
    assert len(calls) == 1
    assert calls[0]["operation"] == "assess"
    assert calls[0]["result_state"] == "success"
    assert calls[0]["actual_usd"] is not None
    assert usage is not None


async def test_llm_input_contains_envelope_contract(harness) -> None:
    """GenerateTurnInput: вопрос с рубрикой, ответ, следующая разрешённая тема,
    остаток уточнений, границы сложности (§3.3)."""
    _session, turn = await start_answered(harness)
    await evaluate(harness)
    data = harness.llm.parsed_inputs[0]
    assert data["turn_id"] == str(turn["id"])
    assert data["question_type"] == "hr"
    assert data["topic"] == "self_introduction"
    assert data.get("rubric")
    assert data["answer"] == turn["answer_text"] or data["answer"]
    assert data["next_allowed_topic"] == "project_story"
    assert data["follow_ups_remaining"] == 2
    assert data["current_difficulty"] == 2
    assert (data["difficulty_min"], data["difficulty_max"]) == (1, 3)
    # системный промпт — из версионируемого файла; ответ — только в user message
    request = harness.llm.calls[0]
    assert "ВЕРСИЯ ПРОМПТА: 2.0" in request.system_prompt
    assert data["answer"] not in request.system_prompt
    assert request.temperature == 0.2
    assert request.max_output_tokens == 1500
    assert request.timeout_seconds == 20


async def test_previous_assessments_accumulate(harness) -> None:
    await start_answered(harness)
    await evaluate(harness)
    await answer_current(harness, "Проект: собирал данные опросов и считал статистику.")
    await evaluate(harness)
    await answer_current(harness, "Знаю list, dict, set; поиск в словаре O(1) через хеш-таблицу.")
    await evaluate(harness)
    third_input = harness.llm.parsed_inputs[2]
    assert len(third_input["previous_assessments"]) == 2


# ---------------------------------------------------------------------------
# Уточнения (§3.3: ≤1 на основной вопрос, ≤2 на сессию)
# ---------------------------------------------------------------------------


async def test_follow_up_created_on_weak_answer(harness) -> None:
    session, turn = await start_answered(harness)
    harness.llm.responder = scripted(
        {
            "topic_score": 2,
            "relevance": 3,
            "missing": ["Не названы конкретные инструменты"],
            "action": "follow_up",
            "next_question": "Уточните: какие именно инструменты вы использовали в учёбе?",
        }
    )
    await evaluate(harness)

    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 2
    follow = turns[1]
    assert follow["kind"] == "follow_up"
    assert follow["parent_turn_id"] == turn["id"]
    assert follow["topic"] == turn["topic"]  # та же тема
    assert follow["main_index"] is None
    assert follow["question_text"].startswith("Уточните")
    updated = await harness.fetch_active_session()
    assert updated["follow_ups_used"] == 1
    assert updated["status"] == "awaiting_answer"


async def test_follow_up_answer_advances_to_next_main(harness) -> None:
    session, turn1 = await start_answered(harness)
    harness.llm.responder = scripted(
        {
            "topic_score": 2,
            "relevance": 3,
            "missing": ["детали"],
            "action": "follow_up",
            "next_question": "Уточните детали вашего учебного опыта?",
        },
        {},  # ответ на уточнение — по умолчанию next_topic
    )
    await evaluate(harness)
    await answer_current(harness, "Использовал Python и pandas для обработки данных.")
    await evaluate(harness)

    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 3
    third = turns[2]
    assert third["kind"] == "main" and third["main_index"] == 2
    assert third["topic"] == "project_story"


async def test_dont_know_answer_skips_follow_up(harness) -> None:
    """Нормализованное «не знаю» -> дальше без уточнения, даже если модель
    просит follow_up (§3.3)."""
    session, turn = await start_answered(harness, answer="не знаю")
    harness.llm.responder = scripted(
        {
            "topic_score": 1,
            "relevance": 2,
            "missing": ["всё"],
            "action": "follow_up",
            "next_question": "Уточните чего вы не знаете?",
        }
    )
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert all(t["kind"] == "main" for t in turns)
    assert len(turns) == 2 and turns[1]["main_index"] == 2


async def test_relevance_zero_forces_next_topic(harness) -> None:
    session, _turn = await start_answered(
        harness, answer="Совершенно посторонний ответ про погоду."
    )
    harness.llm.responder = scripted(
        {
            "topic_score": 0,
            "relevance": 0,
            "missing": ["ответ не по теме"],
            "action": "follow_up",
            "next_question": "Уточните про погоду?",
        }
    )
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 2 and turns[1]["kind"] == "main"


async def test_session_follow_up_limit_is_two(harness) -> None:
    """2 уточнения на сессию: третье слабое miejsce — следующая тема (§1.3)."""
    session, _ = await start_answered(harness)
    weak_follow_up = {
        "topic_score": 2,
        "relevance": 3,
        "missing": ["детали"],
        "action": "follow_up",
    }
    harness.llm.responder = scripted(
        {**weak_follow_up, "next_question": "Уточните подробности вашей учёбы?"},
        {},  # follow_up 1 answered
        {**weak_follow_up, "next_question": "Уточните подробности вашего проекта?"},
        {},  # follow_up 2 answered
        {**weak_follow_up, "next_question": "Уточните подробности про коллекции?"},
    )
    for answer in (
        "Использовал Python и pandas.",           # main1 -> FU1
        "Подробности учёбы: курсы и практика.",    # FU1 -> main2
        "Проект: сбор данных опросов.",            # main2 -> FU2
        "Подробности проекта: этапы и данные.",    # FU2 -> main3
        "Знаю list и dict, поиск O(1).",           # main3 -> FU НЕ создаётся
    ):
        await answer_current(harness, answer)
        await evaluate(harness)

    turns = await harness.fetch_turns(session["id"])
    follow_ups = [t for t in turns if t["kind"] == "follow_up"]
    assert len(follow_ups) == 2
    updated = await harness.fetch_active_session()
    assert updated["follow_ups_used"] == 2


async def test_one_follow_up_per_parent(harness) -> None:
    """Ответ на уточнение с низкой оценкой не создаёт второе уточнение
    к тому же основному вопросу (§2.3)."""
    session, _turn1 = await start_answered(harness)
    harness.llm.responder = scripted(
        {
            "topic_score": 2, "relevance": 3, "missing": ["детали"],
            "action": "follow_up",
            "next_question": "Уточните детали вашей учёбы?",
        },
        {
            "topic_score": 1, "relevance": 2, "missing": ["мало конкретики"],
            "action": "follow_up",
            "next_question": "Ещё одно уточнение про учёбу?",
        },
    )
    await evaluate(harness)
    await answer_current(harness, "Мало конкретики, просто курсы.")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert sum(1 for t in turns if t["kind"] == "follow_up") == 1
    assert turns[-1]["kind"] == "main" and turns[-1]["main_index"] == 2


# ---------------------------------------------------------------------------
# Сложность (§3.3: intern 1–2, junior 1–3, шаг после основного ответа)
# ---------------------------------------------------------------------------


async def test_difficulty_steps_junior(harness) -> None:
    session, _ = await start_answered(harness)
    harness.llm.responder = scripted(
        {},                                   # HR1: сложность не меняется
        {},                                   # HR2
        {"topic_score": 4, "relevance": 4},   # main3 strong: +1 -> 3
        {"topic_score": 1, "relevance": 1},   # main4 weak: -1 -> 2
        {"topic_score": 2, "relevance": 3},   # main5 mid: без шага
    )
    await evaluate(harness)   # -> main2
    await answer_current(harness, "Проект по аналилу данных: собирал и считал.")
    await evaluate(harness)   # -> main3 (collections, difficulty 2)
    turns = await harness.fetch_turns(session["id"])
    collections = next(t for t in turns if t["topic"] == "collections")
    assert collections["difficulty"] == 2

    await answer_current(harness, "dict и set на хеш-таблицах, поиск O(1), list O(n).")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    exceptions = next(t for t in turns if t["topic"] == "exceptions")
    assert exceptions["difficulty"] == 3  # шаг вверх
    assert (await harness.fetch_active_session())["current_difficulty"] == 3

    await answer_current(harness, "Про исключения почти ничего не знаю.")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    scoping = next(t for t in turns if t["topic"] == "scoping")
    assert scoping["difficulty"] == 2  # шаг вниз

    await answer_current(
        harness, "LEGB: local, enclosing, global, built-in, nonlocal для замыканий."
    )
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    data_proc = next(t for t in turns if t["topic"] == "data_processing")
    assert data_proc["difficulty"] == 2  # без шага при оценке 2


async def test_difficulty_clamped_for_intern(harness) -> None:
    session, _ = await start_answered(harness, level="intern")
    harness.llm.responder = scripted(
        {}, {}, {"topic_score": 4, "relevance": 4}, {"topic_score": 4, "relevance": 4},
    )
    await evaluate(harness)
    await answer_current(harness, "Проект: учебный анализ данных.")
    await evaluate(harness)
    await answer_current(harness, "Коллекции: list, dict, set, сложность O(1) для поиска.")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    exceptions = next(t for t in turns if t["topic"] == "exceptions")
    assert exceptions["difficulty"] == 2  # intern: 1 -> 2, не выше
    await answer_current(harness, "Исключения: try/except/finally, конкретные классы.")
    await evaluate(harness)
    updated = await harness.fetch_active_session()
    assert updated["current_difficulty"] == 2  # предел intern


async def test_hr_and_follow_up_do_not_change_difficulty(harness) -> None:
    session, _ = await start_answered(harness)
    harness.llm.responder = scripted(
        {"topic_score": 4, "relevance": 4},  # HR1 strong — техническая сложность не меняется
    )
    await evaluate(harness)
    assert (await harness.fetch_active_session())["current_difficulty"] == 2


# ---------------------------------------------------------------------------
# Вопросы модели и каталога (§3.3)
# ---------------------------------------------------------------------------


async def test_model_question_adopted_for_technical_topic(harness) -> None:
    session, _ = await start_answered(harness)
    model_question = (
        "Объясните сложность поиска в словаре dict и множестве set: "
        "почему хеш-таблица даёт O(1) и что происходит при коллизиях?"
    )
    harness.llm.responder = scripted(
        {},
        {"topic_score": 3, "relevance": 3, "action": "next_topic",
         "next_question": model_question},
    )
    await evaluate(harness)
    await answer_current(harness, "Проект: собирал статистику опросов в Python.")
    await evaluate(harness)  # оценка HR2 -> переход к collections (technical)
    turns = await harness.fetch_turns(session["id"])
    collections = next(t for t in turns if t["topic"] == "collections")
    assert collections["question_text"] == model_question
    job = await harness.fetch_jobs(f"assess:{turns[1]['id']}")
    assert json.loads(job["result"])["question_source"] == "model"


async def test_off_topic_model_question_replaced_by_catalog(harness) -> None:
    session, _ = await start_answered(harness)
    harness.llm.responder = scripted(
        {},
        {"topic_score": 3, "relevance": 3, "action": "next_topic",
         "next_question": "Расскажите про устройство реактивного двигателя и турбины?"},
    )
    await evaluate(harness)
    await answer_current(harness, "Проект: анализ данных опросов студентов.")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    collections = next(t for t in turns if t["topic"] == "collections")
    # вопрос модели отброшен (посторонняя тема) — использован вопрос каталога
    assert "реактивного" not in collections["question_text"]
    assert collections["catalog_entry_id"] == "python-junior-tech-collections"
    scenario = next(
        s for s in harness.config.catalog.scenarios
        if s.role_id.value == "python" and s.level.value == "junior"
    )
    entry = next(e for e in scenario.entries if e.topic == "collections")
    assert collections["question_text"] == entry.question_for_difficulty(
        collections["difficulty"]
    )


async def test_invalid_follow_up_question_falls_back(harness) -> None:
    """Уточнение с недопустимым вопросом (управляющий символ): вывод невалиден,
    повтор, затем запасной сценарий — вопрос из каталога (§3.3)."""
    session, turn = await start_answered(harness)
    harness.llm.responder = scripted(
        {
            "topic_score": 2, "relevance": 3, "missing": ["детали"],
            "action": "follow_up",
            "next_question": "Уточните детали\x07 вашего опыта?",
        },
        {
            "topic_score": 2, "relevance": 3, "missing": ["детали"],
            "action": "follow_up",
            "next_question": "Уточните детали\x07 вашего опыта?",
        },
    )
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert all(t["kind"] == "main" for t in turns)
    assert len(turns) == 2 and turns[1]["main_index"] == 2
    assert any(
        "Интервью продолжается с ограниченной оценкой" in t for t in harness.bot.texts()
    )
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job["state"] == "failed"
    assert job["error_code"] == "AI_INVALID_OUTPUT"
    assert len(harness.llm.calls) == 2  # не более двух вызовов на операцию


# ---------------------------------------------------------------------------
# Запасные сценарии (§3.2, AT08-частично)
# ---------------------------------------------------------------------------


async def test_invalid_json_retry_then_success(harness) -> None:
    session, turn = await start_answered(harness)
    harness.llm.responder = scripted("это не json", {})
    await evaluate(harness)
    assert len(harness.llm.calls) == 2
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is not None  # второй вызов успешен
    assert (await harness.fetch_active_session())["status"] == "awaiting_answer"


async def test_invalid_json_twice_falls_back_to_catalog(harness) -> None:
    session, turn = await start_answered(harness)
    harness.llm.responder = scripted("мусор", "тоже мусор")
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None  # оценка недоступна
    assert len(turns) == 2 and turns[1]["kind"] == "main"  # интервью продолжается
    assert any(
        "Интервью продолжается с ограниченной оценкой" in t for t in harness.bot.texts()
    )
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job["state"] == "failed" and job["error_code"] == "AI_INVALID_OUTPUT"
    assert len(harness.llm.calls) == 2


async def test_fabricated_quote_rejected(harness) -> None:
    """Цитата, которой нет в ответе: вывод невалиден (§3.3), запасной сценарий."""
    session, turn = await start_answered(harness, answer="Короткий честный ответ.")

    def fabricated(data, call_no):
        base = FakeLlm.default_assessment(data)
        base["evidence_quotes"] = ["выдуманная цитата, которой нет в ответе"]
        return json.dumps(base, ensure_ascii=False)

    harness.llm.responder = scripted(fabricated, fabricated)
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None
    assert len(harness.llm.calls) == 2


async def test_config_error_no_retry_and_fallback(harness) -> None:
    session, turn = await start_answered(harness)
    harness.llm.responder = lambda data, n: LlmConfigError(status=401)
    await evaluate(harness)
    assert len(harness.llm.calls) == 1  # 401 не повторяется (§3.2)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None
    assert len(turns) == 2  # следующий вопрос из каталога
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job["state"] == "failed" and job["error_code"] == "AI_UNAVAILABLE"


async def test_retryable_error_then_success(harness) -> None:
    session, turn = await start_answered(harness)
    harness.llm.responder = scripted(LlmRetryableError(status=503), {})
    await evaluate(harness)
    assert len(harness.llm.calls) == 2
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is not None
    async with harness.pool.acquire() as conn:
        calls = await conn.fetch("SELECT result_state FROM ai_calls ORDER BY attempt")
    assert [c["result_state"] for c in calls] == ["known_failure", "success"]


async def test_unknown_result_moves_reserve_to_unknown(harness, db_url, tmp_path_factory) -> None:
    """Неизвестный результат учитывается как unknown_usd до сверки (§3.2, §3.10)."""
    import json as _json

    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("costs")
    settings = _json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
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
    custom.write_text(_json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        ),
    )
    session, turn = await start_answered(local)
    local.llm.responder = lambda data, n: LlmUnknownResultError("timeout")
    await local.run_jobs()
    await local.drain_outbox()

    async with local.pool.acquire() as conn:
        usage = await conn.fetchrow("SELECT * FROM daily_usage")
        calls = await conn.fetch("SELECT result_state, actual_usd FROM ai_calls")
    assert len(calls) == 2
    assert all(c["result_state"] == "unknown" for c in calls)
    assert all(c["actual_usd"] is None for c in calls)
    from decimal import Decimal

    assert usage["unknown_usd"] > Decimal(0)
    assert usage["reserved_usd"] == Decimal(0)  # резерв переведён в unknown
    # запасной сценарий применён
    turns = await local.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None and len(turns) == 2


async def test_success_cost_replaces_reserve(harness, db_url, tmp_path_factory) -> None:
    import json as _json
    from decimal import Decimal

    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("costs2")
    settings = _json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 1000
    settings["costs"].update(
        {
            "daily_ai_budget_usd": 100,
            "llm_input_per_1k_usd": 0.01,
            "llm_output_per_1k_usd": 0.03,
        }
    )
    custom = cfg_dir / "settings.json"
    custom.write_text(_json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        ),
    )
    session, turn = await start_answered(local)
    await local.run_jobs()
    async with local.pool.acquire() as conn:
        usage = await conn.fetchrow("SELECT * FROM daily_usage")
    assert usage["known_usd"] > Decimal(0)
    assert usage["reserved_usd"] == Decimal(0)
    assert usage["unknown_usd"] == Decimal(0)


async def test_daily_budget_blocks_expensive_call(harness, db_url, tmp_path_factory) -> None:
    import json as _json

    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("budget")
    settings = _json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 1000
    settings["costs"].update(
        {
            "daily_ai_budget_usd": 0.000001,
            "llm_input_per_1k_usd": 0.01,
            "llm_output_per_1k_usd": 0.03,
        }
    )
    custom = cfg_dir / "settings.json"
    custom.write_text(_json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        ),
    )
    session, turn = await start_answered(local)
    await local.run_jobs()
    await local.drain_outbox()
    assert local.llm.calls == []  # вызов заблокирован суточным лимитом
    turns = await local.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None and len(turns) == 2  # запасной сценарий


async def test_session_token_budget_goes_to_reporting(harness, db_url, tmp_path_factory) -> None:
    """BUDGET_EXCEEDED: резерв отчёта не тратится на оценку — готовим разбор
    уже полученных ответов (§1.3, §3.2)."""
    import json as _json

    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("tbudget")
    settings = _json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 1000
    settings["tokens_budget"]["session_input_budget"] = 25000  # резерв отчёта 24000
    custom = cfg_dir / "settings.json"
    custom.write_text(_json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        ),
    )
    session, turn = await start_answered(local)
    await local.run_jobs()

    # Оценка заблокирована бюджетом; LLM вызывался только для отчёта (резерв
    # 24000 input принадлежит отчёту и расходуется им, §1.3)
    assert all(c.operation == "report" for c in local.llm.calls)
    updated = await local.fetch_active_session()
    assert updated["status"] == "reporting"
    async with local.pool.acquire() as conn:
        report_job = await conn.fetchrow(
            "SELECT * FROM jobs WHERE unique_key = $1", f"report:{session['id']}"
        )
        assess_job = await conn.fetchrow(
            "SELECT state, error_code FROM jobs WHERE unique_key = $1", f"assess:{turn['id']}"
        )
    assert report_job is not None and report_job["state"] == "done"
    assert assess_job["state"] == "failed"
    assert assess_job["error_code"] == "BUDGET_EXCEEDED"
    # до доставки отчёта резерв попытки остаётся за сессией
    credits = await local.fetch_credits()
    assert credits[0]["state"] == "reserved"

    await local.drain_outbox()
    assert any(
        "Подготовим разбор уже полученных ответов" in t for t in local.bot.texts()
    )
    final = await local.fetch_session(session["id"])
    assert final["status"] == "completed"
    credits = await local.fetch_credits()
    assert credits[0]["state"] == "consumed"


async def test_queue_timeout_fallback_without_call(harness) -> None:
    session, _turn = await start_answered(harness)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET created_at = created_at - interval '61 seconds'"
        )
    await evaluate(harness)
    assert harness.llm.calls == []  # очередь AI > 60 с -> запасной сценарий (§1.3)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None and len(turns) == 2


async def test_ai_disabled_profile_falls_back(harness, db_url) -> None:
    """Незаполненный provider-profile: AI-вызовы заблокированы (§1.4)."""
    from tests.conftest import make_env
    from tests.fakes import Harness

    local = Harness(harness.pool, make_env(DATABASE_URL=db_url))
    session, turn = await start_answered(local)
    await local.run_jobs()
    await local.drain_outbox()
    assert local.llm.calls == []
    turns = await local.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None
    assert len(turns) == 2  # интервью продолжается вопросом из каталога


# ---------------------------------------------------------------------------
# Гонки, версии, дедупликация (§2.4, AT05/AT06-частично)
# ---------------------------------------------------------------------------


async def test_late_result_of_stale_version_discarded(harness) -> None:
    session, turn = await start_answered(harness)
    async with harness.pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET version = version + 1 WHERE id = $1", session["id"]
        )
    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert turns[0]["assessment"] is None  # результат отброшен
    assert len(turns) == 1                  # новый вопрос не создан
    job = await harness.fetch_jobs(f"assess:{turn['id']}")
    assert job["state"] == "done"
    assert json.loads(job["result"])["stale"] is True


async def test_cancelled_session_result_not_applied(harness) -> None:
    """Поздний AI-результат не восстанавливает отменённую сессию (§2.4)."""
    session, turn = await start_answered(harness)
    await harness.cancel_active()
    await harness.run_jobs()  # задание отменено вместе с сессией
    await harness.drain_outbox()
    async with harness.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM sessions WHERE id = $1", session["id"]
        )
        job = await conn.fetchrow(
            "SELECT state FROM jobs WHERE unique_key = $1", f"assess:{turn['id']}"
        )
        assessment = await conn.fetchval(
            "SELECT assessment FROM turns WHERE id = $1", turn["id"]
        )
    assert row["status"] == "cancelled"
    assert job["state"] == "cancelled"
    assert assessment is None


async def test_finish_during_evaluation_applies_then_reports(harness) -> None:
    """«Завершить» во время оценки: requested_action, оценка применяется,
    новая тема не создаётся, сессия уходит в отчёт (§2.4)."""
    session, turn = await start_answered(harness)
    await harness.click(await harness.button_for("finish_session"))
    active = await harness.fetch_active_session()
    assert active["requested_action"] == "finish"

    await evaluate(harness)
    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 1  # новая тема не создана
    assert turns[0]["assessment"] is not None  # оценка применена
    async with harness.pool.acquire() as conn:
        report_job = await conn.fetchrow(
            "SELECT state FROM jobs WHERE unique_key = $1", f"report:{session['id']}"
        )
        status = await conn.fetchval(
            "SELECT status FROM sessions WHERE id = $1", session["id"]
        )
    # часть 4: отчёт исполняется и доставляется — сессия завершена,
    # ранний финиш даёт partial=true (§3.4)
    assert report_job["state"] == "done"
    assert status == "completed"
    report = await harness.fetch_report(session["id"])
    assert report["partial"] is True
    assert any(messages.PREPARING_REPORT in t for t in harness.bot.texts())


async def test_provider_calls_capped_at_two_across_reruns(harness, monkeypatch) -> None:
    """Общий предел 2 вызова на операцию, включая повтор после сбоя (§3.2)."""
    session, turn = await start_answered(harness)

    def crasher(data, call_no):
        if call_no == 1:
            raise LlmUnknownResultError("timeout")
        raise RuntimeError("worker crash")

    harness.llm.responder = crasher
    await harness.run_jobs(max_items=1)
    # после неожиданного сбоя задание возвращено в очередь
    async with harness.pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT state, attempts FROM jobs WHERE unique_key = $1", f"assess:{turn['id']}"
        )
    assert job["state"] == "pending"

    harness.llm.responder = lambda data, n: json.dumps(
        FakeLlm.default_assessment(data), ensure_ascii=False
    )
    await harness.run_jobs()
    await harness.drain_outbox()
    # третий вызов не выполнен: лимит 2 на операцию исчерпан в первом прогоне
    assert len(harness.llm.calls) == 2
    async with harness.pool.acquire() as conn:
        calls = await conn.fetchval(
            "SELECT count(*) FROM ai_calls WHERE job_id = "
            "(SELECT id FROM jobs WHERE unique_key=$1)",
            f"assess:{turn['id']}",
        )
    assert calls == 2


# ---------------------------------------------------------------------------
# Полный план интервью (AT02-механика)
# ---------------------------------------------------------------------------


async def test_full_text_interview_reaches_reporting(harness) -> None:
    """6 основных вопросов без уточнений -> отчёт; один credit зарезервирован
    до доставки отчёта (§1.3, §2.2)."""
    session, _ = await start_answered(harness)
    await evaluate(harness)  # оценка HR1 -> вопрос 2
    answers = [
        "Проект: собирал данные опросов студентов и считал статистику в Python.",
        "Коллекции: list изменяемый, tuple нет, dict и set дают O(1) для поиска.",
        "Исключения: try/except/else/finally, перехватываю конкретные классы.",
        "Области видимости: LEGB, nonlocal для замыканий, global для модуля.",
        "Данные: читаю построчно через with open, фильтрую генераторным выражением.",
    ]
    for index, answer in enumerate(answers):
        active = await harness.fetch_active_session()
        assert active is not None and active["status"] == "awaiting_answer", (index, active)
        await answer_current(harness, answer)
        await evaluate(harness)

    turns = await harness.fetch_turns(session["id"])
    assert len(turns) == 6
    assert all(t["kind"] == "main" for t in turns)
    assert all(t["assessment"] is not None for t in turns)
    assert [t["main_index"] for t in turns] == [1, 2, 3, 4, 5, 6]
    async with harness.pool.acquire() as conn:
        calls = await conn.fetchval("SELECT count(*) FROM ai_calls")
        report_job = await conn.fetchrow(
            "SELECT state FROM jobs WHERE unique_key = $1", f"report:{session['id']}"
        )
        status = await conn.fetchval(
            "SELECT status FROM sessions WHERE id = $1", session["id"]
        )
    assert calls == 7  # 6 оценок + 1 генерация отчёта
    assert report_job["state"] == "done"
    assert status == "completed"  # части отчёта доставлены в evaluate()
    report = await harness.fetch_report(session["id"])
    assert report["partial"] is False and report["degraded"] is False
    parts = report["parts"]
    if isinstance(parts, str):
        import json as _json

        parts = _json.loads(parts)
    assert 1 <= len(parts) <= 4
    assert all(len(part) <= 3500 for part in parts)
    assert report["total_chars"] <= 12000
    # попытка списана после подтверждения всех частей (§3.8)
    credits = await harness.fetch_credits()
    assert credits[0]["state"] == "consumed"
    assert credits[0]["consumed_session_id"] == session["id"]


async def test_loading_status_updated_once_after_threshold(
    harness, db_url, tmp_path_factory
) -> None:
    """Интерфейс показывает ожидание: после порога существующий статус
    обновляется один раз (§3.7)."""
    import json as _json

    from tests.conftest import ROOT, make_env, write_filled_provider_profile
    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("loading")
    settings = _json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    settings["limits"]["commands_per_minute"] = 1000
    settings["ui"]["loading_update_after_seconds"] = 1
    custom = cfg_dir / "settings.json"
    custom.write_text(_json.dumps(settings), encoding="utf-8")
    local = Harness(
        harness.pool,
        make_env(
            DATABASE_URL=db_url,
            SETTINGS_PATH=str(custom),
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        ),
    )
    local.llm.delay = 1.4
    session, turn = await start_answered(local)
    await local.run_jobs()
    await local.drain_outbox()
    assert any(
        text == messages.PROCESSING_TAKES_LONGER for _c, _m, text in local.bot.edits
    ), local.bot.edits
    assert len(local.bot.edits) == 1  # максимум один раз
