"""Контроль состояния и сигналы оператору (§3.10; задача T19).

Периодический снимок очередей из PostgreSQL + вычисление сигналов:
- oldest AI job более 60 секунд;
- degraded более 10% за последние 20 отчётов;
- отсутствие heartbeat worker более 60 секунд;
- ошибка backup (статус-файл отсутствует, устарел более 26 ч или ok=false);
- 401/403 AI (config_error провайдера с прошлой проверки);
- остаток суточного бюджета менее 20%.

Реальные получатель и канал уведомлений задаются перед запуском (условие
запуска §3.10): здесь сигналы пишутся структурированным логом
`operator_signal` уровня ERROR. Содержимое ответов в метрики не попадает.

Heartbeat worker — файл в RUN_DIR (схема БД фиксирована: 14 таблиц, §2.3).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from app.config import AppConfig
from app.services import metrics

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = "worker_heartbeat.json"
BACKUP_STATUS_FILE = "backup_status.json"
BACKUP_MAX_AGE_HOURS = 26  # ежедневный backup + запас (§3.10: RPO до 24 ч)


# ---------------------------------------------------------------------------
# Heartbeat: запись (worker) и чтение (мониторинг)
# ---------------------------------------------------------------------------


def ensure_run_dir(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # pragma: no cover - зависит от ФС
        os.chmod(run_dir, 0o700)


def write_heartbeat(run_dir: Path) -> None:
    """Один удар сердца worker-процесса (интервал worker_heartbeat < 60 с)."""
    ensure_run_dir(run_dir)
    payload = {
        "pid": os.getpid(),
        "ts": datetime.now(UTC).isoformat(),
        "monotonic": time.monotonic(),
    }
    path = run_dir / HEARTBEAT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def heartbeat_age_seconds(run_dir: Path) -> float | None:
    """Возраст последнего heartbeat; None если файла нет."""
    path = run_dir / HEARTBEAT_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["ts"])
        return max(0.0, (datetime.now(UTC) - ts).total_seconds())
    except (OSError, ValueError, KeyError):
        return None


def write_backup_status(run_dir: Path, *, ok: bool, detail: str = "") -> None:
    """Статус последней резервной копии (пишет scripts/backup.sh)."""
    ensure_run_dir(run_dir)
    payload = {
        "ok": ok,
        "detail": detail[:200],
        "ts": datetime.now(UTC).isoformat(),
    }
    (run_dir / BACKUP_STATUS_FILE).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def read_backup_status(run_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((run_dir / BACKUP_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Снимок состояния из БД (без содержимого ответов)
# ---------------------------------------------------------------------------


async def collect_gauges(conn: asyncpg.Connection, config: AppConfig) -> dict[str, Any]:
    inbox = await conn.fetchrow(
        """
        SELECT count(*) AS pending,
               COALESCE(EXTRACT(EPOCH FROM (now() - min(received_at))), 0) AS oldest_seconds
        FROM inbox WHERE status IN ('pending', 'processing')
        """
    )
    jobs = await conn.fetchrow(
        """
        SELECT count(*) AS pending,
               COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) AS oldest_seconds
        FROM jobs WHERE state IN ('pending', 'leased')
        """
    )
    outbox = await conn.fetchrow(
        """
        SELECT count(*) AS pending,
               COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) AS oldest_seconds
        FROM outbox WHERE state IN ('pending', 'sending')
        """
    )
    lease_expired = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE state = 'leased' AND lease_expires_at < now()"
    )
    degraded = await conn.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE degraded) AS degraded
        FROM (SELECT degraded FROM reports ORDER BY created_at DESC LIMIT $1) r
        """,
        config.settings.alerts.degraded_report_window,
    )
    usage = await conn.fetchrow(
        """
        SELECT reserved_usd, known_usd, unknown_usd
        FROM daily_usage WHERE day = (now() AT TIME ZONE 'UTC')::date
        """
    )
    return {
        "inbox_pending": inbox["pending"],
        "inbox_oldest_seconds": float(inbox["oldest_seconds"]),
        "jobs_pending": jobs["pending"],
        "ai_oldest_job_seconds": float(jobs["oldest_seconds"]),
        "outbox_pending": outbox["pending"],
        "outbox_oldest_seconds": float(outbox["oldest_seconds"]),
        "lease_expired_jobs": lease_expired,
        "reports_window_total": degraded["total"],
        "reports_window_degraded": degraded["degraded"],
        "daily_reserved_usd": float(usage["reserved_usd"]) if usage else 0.0,
        "daily_known_usd": float(usage["known_usd"]) if usage else 0.0,
        "daily_unknown_usd": float(usage["unknown_usd"]) if usage else 0.0,
        "heartbeat_age_seconds": heartbeat_age_seconds(config.env.run_dir),
        "backup_status": read_backup_status(config.env.run_dir),
    }


# ---------------------------------------------------------------------------
# Сигналы оператору (§3.10)
# ---------------------------------------------------------------------------


class SignalState:
    """Память мониторинга между проверками (приращение 401/403)."""

    def __init__(self) -> None:
        self.last_config_errors = 0


def evaluate_signals(
    gauges: dict[str, Any],
    config: AppConfig,
    state: SignalState,
    *,
    config_errors_total: int | None = None,
) -> list[dict[str, Any]]:
    """Возвращает список сигналов; каждый — категория + числа (без контента)."""
    alerts = config.settings.alerts
    signals: list[dict[str, Any]] = []

    if gauges["ai_oldest_job_seconds"] > alerts.oldest_ai_job_seconds:
        signals.append(
            {
                "signal": "oldest_ai_job_over_threshold",
                "age_seconds": round(gauges["ai_oldest_job_seconds"], 1),
                "threshold_seconds": alerts.oldest_ai_job_seconds,
            }
        )

    total = gauges["reports_window_total"]
    if total >= alerts.degraded_report_window:
        ratio = gauges["reports_window_degraded"] / total
        if ratio > alerts.degraded_report_ratio_max:
            signals.append(
                {
                    "signal": "degraded_reports_ratio_high",
                    "ratio": round(ratio, 3),
                    "window": total,
                    "max_ratio": alerts.degraded_report_ratio_max,
                }
            )

    heartbeat_age = gauges["heartbeat_age_seconds"]
    if heartbeat_age is None or heartbeat_age > alerts.worker_heartbeat_missing_seconds:
        signals.append(
            {
                "signal": "worker_heartbeat_missing",
                "age_seconds": None if heartbeat_age is None else round(heartbeat_age, 1),
                "threshold_seconds": alerts.worker_heartbeat_missing_seconds,
            }
        )

    backup = gauges["backup_status"]
    backup_age_hours = None
    if backup is not None:
        try:
            ts = datetime.fromisoformat(backup["ts"])
            backup_age_hours = (datetime.now(UTC) - ts).total_seconds() / 3600.0
        except (ValueError, KeyError):
            backup_age_hours = None
    if (
        backup is None
        or not backup.get("ok")
        or backup_age_hours is None
        or backup_age_hours > BACKUP_MAX_AGE_HOURS
    ):
        signals.append(
            {
                "signal": "backup_error",
                "status_known": backup is not None,
                "ok": bool(backup.get("ok")) if backup else False,
                "age_hours": None if backup_age_hours is None else round(backup_age_hours, 1),
            }
        )

    if config_errors_total is None:
        config_errors_total = sum(
            v
            for k, v in metrics.REGISTRY.snapshot()["counters"].items()
            if k.startswith(metrics.PROVIDER_CALLS)
            and f"outcome={metrics.OUTCOME_CONFIG_ERROR}" in k
        )
    if config_errors_total > state.last_config_errors:
        signals.append(
            {
                "signal": "ai_auth_error_401_403",
                "new_since_last_check": config_errors_total - state.last_config_errors,
            }
        )
    state.last_config_errors = config_errors_total

    budget = config.settings.costs.daily_ai_budget_usd
    if budget and budget > 0:
        spent = (
            gauges["daily_reserved_usd"]
            + gauges["daily_known_usd"]
            + gauges["daily_unknown_usd"]
        )
        remainder_ratio = max(0.0, (budget - spent) / budget)
        if remainder_ratio < config.settings.costs.budget_low_remainder_alert_ratio:
            signals.append(
                {
                    "signal": "daily_budget_low",
                    "remainder_ratio": round(remainder_ratio, 3),
                    "alert_below": config.settings.costs.budget_low_remainder_alert_ratio,
                }
            )
    return signals


def emit_signals(signals: list[dict[str, Any]]) -> None:
    """Канал уведомлений задаётся перед запуском (§3.10): пока — ERROR-лог
    со структурированными полями, без содержимого ответов."""
    for signal in signals:
        logger.error("operator_signal %s", json.dumps(signal, ensure_ascii=False))
