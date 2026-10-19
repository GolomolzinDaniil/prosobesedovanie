"""Мониторинг и сигналы оператору (§3.10; задача T19).

Снимок очередей из БД, оценка шести сигналов (oldest AI job, доля degraded,
heartbeat worker, backup, 401/403 AI, остаток суточного бюджета) и сквозной
учёт попыток API/ошибок через полный конвейер harness-а.
"""

from __future__ import annotations

import json

import pytest

from app.services import metrics, monitoring

pytestmark = pytest.mark.asyncio

ANSWER = "Учусь на третьем курсе, пишу учебный проект по анализу данных."


# ---------------------------------------------------------------------------
# Сквозной учёт: все попытки API и сигналы ошибок (§3.10 «учёт всех попыток»)
# ---------------------------------------------------------------------------


async def test_pipeline_increments_core_counters(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()
    await harness.drain_outbox()

    snap = metrics.REGISTRY.snapshot()
    counters = snap["counters"]
    # inbox обработка
    assert counters.get("inbox_processed_total{status=processed}", 0) >= 3
    # AI-вызов оценки учтён как успех
    assert counters.get(
        "provider_calls_total{operation=assess,outcome=success}", 0
    ) >= 1
    # доставки outbox
    assert any(
        k.startswith("outbox_sent_total{") and "outcome=success" in k
        for k in counters
    )
    # задержки этапов наблюдались
    assert any(name.startswith("latency_inbox_seconds") for name in snap["latency"])
    assert any(name.startswith("latency_job_seconds") for name in snap["latency"])
    assert any(name.startswith("latency_outbox_seconds") for name in snap["latency"])
    # стоимость: резерв и факт учтены в микродолларах
    assert any(k.startswith(metrics.COST_RESERVED_USD) for k in counters)
    assert counters.get(metrics.COST_ACTUAL_USD, 0) >= 0


async def test_invalid_json_outcome_counted(harness) -> None:
    """Доля невалидного JSON видна в счётчиках (без содержимого)."""
    calls = {"n": 0}

    def responder(data: dict, call_no: int) -> str:
        if "snapshot_turns" in data:
            from tests.fakes import FakeLlm

            return json.dumps(FakeLlm.default_report(data), ensure_ascii=False)
        calls["n"] += 1
        if calls["n"] == 1:
            return "{не json"
        from tests.fakes import FakeLlm

        return json.dumps(FakeLlm.default_assessment(data), ensure_ascii=False)

    harness.llm.responder = responder
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.run_jobs()

    counters = metrics.REGISTRY.snapshot()["counters"]
    assert counters.get(
        "provider_calls_total{operation=assess,outcome=invalid_json}", 0
    ) == 1
    assert counters.get("provider_retries_total{operation=assess}", 0) >= 1


async def test_fallback_and_degraded_counters(harness) -> None:
    """AI недоступен -> fallback assess; отчёт degraded учитывается."""
    harness.llm = None
    harness.deps.llm = None
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_text(ANSWER, reply_to_message_id=turn["delivered_message_id"])
    await harness.click(await harness.button_for("finish_session"))
    await harness.run_jobs()
    await harness.drain_outbox()

    counters = metrics.REGISTRY.snapshot()["counters"]
    assert any(
        k.startswith("fallback_total{operation=assess") for k in counters
    )
    assert counters.get(metrics.REPORT_DEGRADED, 0) >= 1
    assert any(
        k.startswith("fallback_total{operation=report") for k in counters
    )
    # успех report-delivery после доставки всех частей
    assert counters.get(
        f"report_delivered_total{{outcome={metrics.OUTCOME_SUCCESS}}}", 0
    ) >= 1


async def test_stt_action_counters(harness) -> None:
    """STT-подтверждения и перезаписи (§3.10)."""
    await harness.complete_entry(fmt="voice")
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    await harness.send_voice(reply_to=turn["delivered_message_id"], duration=10,
                             file_id="voice-metrics-1")
    await harness.run_jobs()
    await harness.drain_outbox()
    # перезапись
    await harness.click(await harness.button_for("rerecord_voice"))
    counters = metrics.REGISTRY.snapshot()["counters"]
    assert counters.get("stt_action_total{action=rerecorded}", 0) == 1
    # повторная запись и подтверждение
    session = await harness.fetch_active_session()
    turn = next(
        t for t in await harness.fetch_turns(session["id"])
        if t["id"] == session["current_turn_id"]
    )
    await harness.send_voice(reply_to=turn["delivered_message_id"], duration=10,
                             file_id="voice-metrics-2")
    await harness.run_jobs()
    await harness.drain_outbox()
    await harness.click(await harness.button_for("confirm_transcript"))
    counters = metrics.REGISTRY.snapshot()["counters"]
    assert counters.get("stt_action_total{action=confirmed}", 0) == 1


# ---------------------------------------------------------------------------
# Снимок состояния и сигналы (§3.10)
# ---------------------------------------------------------------------------


async def test_collect_gauges_empty_db(harness) -> None:
    async with harness.pool.acquire() as conn:
        gauges = await monitoring.collect_gauges(conn, harness.config)
    assert gauges["inbox_pending"] == 0
    assert gauges["jobs_pending"] == 0
    assert gauges["ai_oldest_job_seconds"] == 0.0
    assert gauges["lease_expired_jobs"] == 0
    assert gauges["heartbeat_age_seconds"] is None  # файла нет
    assert gauges["backup_status"] is None


async def test_collect_gauges_sees_old_ai_job(harness) -> None:
    await harness.complete_entry()
    session = await harness.fetch_active_session()
    async with harness.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs (unique_key, session_id, kind, state, payload,
                              expected_version, created_at)
            VALUES ('gauge-test', $1, 'report', 'pending', '{}'::jsonb, 1,
                    now() - interval '5 minutes')
            """,
            session["id"],
        )
        gauges = await monitoring.collect_gauges(conn, harness.config)
    assert gauges["jobs_pending"] == 1
    assert gauges["ai_oldest_job_seconds"] > 60


async def test_heartbeat_file_roundtrip(harness, tmp_path) -> None:
    run_dir = tmp_path / "run"
    monitoring.write_heartbeat(run_dir)
    age = monitoring.heartbeat_age_seconds(run_dir)
    assert age is not None and age < 5
    assert monitoring.heartbeat_age_seconds(tmp_path / "missing") is None


async def test_evaluate_signals_fires_all_categories(harness) -> None:
    config = harness.config
    state = monitoring.SignalState()
    gauges = {
        "ai_oldest_job_seconds": 120.0,          # > 60
        "reports_window_total": 20,
        "reports_window_degraded": 10,           # 50% > 10%
        "heartbeat_age_seconds": None,           # отсутствует
        "backup_status": None,                   # нет статуса
        "daily_reserved_usd": 0.0,
        "daily_known_usd": 0.0,
        "daily_unknown_usd": 0.0,
    }
    signals = monitoring.evaluate_signals(
        gauges, config, state, config_errors_total=3
    )
    names = {s["signal"] for s in signals}
    assert "oldest_ai_job_over_threshold" in names
    assert "degraded_reports_ratio_high" in names
    assert "worker_heartbeat_missing" in names
    assert "backup_error" in names
    assert "ai_auth_error_401_403" in names
    # бюджет не задан (daily_ai_budget_usd=None) — сигнала нет
    assert "daily_budget_low" not in names
    # повторная проверка без приращения 401/403 — сигнал не повторяется
    again = monitoring.evaluate_signals(gauges, config, state, config_errors_total=3)
    assert "ai_auth_error_401_403" not in {s["signal"] for s in again}


async def test_evaluate_signals_quiet_when_healthy(harness, tmp_path) -> None:
    config = harness.config
    monitoring.write_heartbeat(config.env.run_dir)
    monitoring.write_backup_status(config.env.run_dir, ok=True)
    state = monitoring.SignalState()
    gauges = {
        "ai_oldest_job_seconds": 5.0,
        "reports_window_total": 20,
        "reports_window_degraded": 1,            # 5% < 10%
        "heartbeat_age_seconds": monitoring.heartbeat_age_seconds(config.env.run_dir),
        "backup_status": monitoring.read_backup_status(config.env.run_dir),
        "daily_reserved_usd": 0.0,
        "daily_known_usd": 0.0,
        "daily_unknown_usd": 0.0,
    }
    signals = monitoring.evaluate_signals(gauges, config, state, config_errors_total=0)
    assert signals == []


async def test_backup_error_signal_on_failed_status(harness) -> None:
    config = harness.config
    monitoring.write_backup_status(config.env.run_dir, ok=False, detail="disk full")
    gauges = {
        "ai_oldest_job_seconds": 0.0,
        "reports_window_total": 0,
        "reports_window_degraded": 0,
        "heartbeat_age_seconds": 0.0,
        "backup_status": monitoring.read_backup_status(config.env.run_dir),
        "daily_reserved_usd": 0.0,
        "daily_known_usd": 0.0,
        "daily_unknown_usd": 0.0,
    }
    signals = monitoring.evaluate_signals(
        gauges, config, monitoring.SignalState(), config_errors_total=0
    )
    backup_signals = [s for s in signals if s["signal"] == "backup_error"]
    assert backup_signals and backup_signals[0]["ok"] is False


async def test_daily_budget_low_signal(harness, tmp_path_factory) -> None:
    """Остаток суточного бюджета менее 20% -> сигнал (§3.10)."""
    from datetime import UTC, datetime

    from app.config import load_config
    from tests.conftest import ROOT, make_env, write_filled_provider_profile

    cfg_dir = tmp_path_factory.mktemp("cfg-budget")
    settings = json.loads(
        (ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    settings["costs"]["daily_ai_budget_usd"] = 10.0
    (cfg_dir / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8"
    )
    profile = write_filled_provider_profile(cfg_dir)
    env = make_env(SETTINGS_PATH=str(cfg_dir / "settings.json"),
                   PROVIDER_PROFILE_PATH=profile)
    config = load_config(env)

    gauges = {
        "ai_oldest_job_seconds": 0.0,
        "reports_window_total": 0,
        "reports_window_degraded": 0,
        "heartbeat_age_seconds": 0.0,
        "backup_status": {"ok": True, "ts": datetime.now(UTC).isoformat()},
        "daily_reserved_usd": 4.0,
        "daily_known_usd": 4.5,
        "daily_unknown_usd": 0.0,   # 8.5 из 10 -> остаток 15% < 20%
    }
    signals = monitoring.evaluate_signals(
        gauges, config, monitoring.SignalState(), config_errors_total=0
    )
    low = [s for s in signals if s["signal"] == "daily_budget_low"]
    assert low and low[0]["remainder_ratio"] == pytest.approx(0.15, abs=0.01)


async def test_emit_signals_logs_without_content(harness, caplog) -> None:
    import logging

    with caplog.at_level(logging.ERROR, logger="app.services.monitoring"):
        monitoring.emit_signals([{"signal": "backup_error", "ok": False}])
    assert any("operator_signal" in r.message for r in caplog.records)
