"""Smoke-проверка развёрнутого сервиса (§3.10, T23; сценарий AT17).

Проверяет: health live/ready, свежесть heartbeat worker, отсутствие
зависших заданий и возраста очередей, (опционально) установленный webhook.
Тест обоих форматов интервью и отчёта выполняется вручную тестовым
Telegram-аккаунтом по planning/deploy-checklist.md (пункт 7).

Использование:
    python scripts/smoke.py [--api-base http://127.0.0.1:8080] [--webhook]

Код возврата: 0 — smoke пройден; 1 — провал; 2 — ошибка окружения.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:500]
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode("utf-8", "replace")[:500] if exc.fp else "")
    except OSError as exc:
        return 0, f"{type(exc).__name__}"


async def _db_checks() -> dict:
    import asyncpg

    from app.config import load_config
    from app.services import monitoring

    config = load_config()
    conn = await asyncpg.connect(config.env.database_url)
    try:
        gauges = await monitoring.collect_gauges(conn, config)
        signals = monitoring.evaluate_signals(
            gauges, config, monitoring.SignalState(), config_errors_total=0
        )
    finally:
        await conn.close()
    signal_names = sorted(s["signal"] for s in signals)
    return {
        "heartbeat_age_seconds": gauges["heartbeat_age_seconds"],
        "jobs_pending": gauges["jobs_pending"],
        "ai_oldest_job_seconds": round(gauges["ai_oldest_job_seconds"], 1),
        "inbox_pending": gauges["inbox_pending"],
        "lease_expired_jobs": gauges["lease_expired_jobs"],
        "signals": signal_names,
    }


def _webhook_ok(config) -> tuple[bool, str]:
    token = config.env.bot_token
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getWebhookInfo", method="GET"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    url = (data.get("result") or {}).get("url") or ""
    expected_route = f"/telegram/webhook/{config.env.webhook_route_token}"
    return expected_route in url, url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--webhook", action="store_true", help="проверить getWebhookInfo")
    args = parser.parse_args()

    failures: list[str] = []

    status, _ = _get(f"{args.api_base}/health/live")
    print(f"health/live: {status}")
    if status != 200:
        failures.append("health/live != 200")

    status, body = _get(f"{args.api_base}/health/ready")
    print(f"health/ready: {status}")
    if status != 200:
        failures.append(f"health/ready != 200: {body[:200]}")

    try:
        db = asyncio.run(_db_checks())
        print(f"db: {json.dumps(db, ensure_ascii=False)}")
        if db["lease_expired_jobs"]:
            failures.append("зависшие lease у jobs")
        # backup_error/heartbeat_missing критичны; бюджет/сигналы AI — предупреждение
        for critical in ("worker_heartbeat_missing", "backup_error"):
            if critical in db["signals"]:
                failures.append(f"сигнал: {critical}")
    except Exception as exc:
        print(f"db checks failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        failures.append("db checks unavailable")

    if args.webhook:
        try:
            from app.config import load_config

            ok, url = _webhook_ok(load_config())
            print(f"webhook: {'ok' if ok else 'MISMATCH'} {url}")
            if not ok:
                failures.append("webhook не установлен/не совпадает route")
        except Exception as exc:
            print(f"webhook check failed: {type(exc).__name__}", file=sys.stderr)
            failures.append("webhook check failed")

    if failures:
        print("\nSMOKE FAILED:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
