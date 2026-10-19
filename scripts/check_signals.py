"""Однократная проверка операторских сигналов (§3.10; задача T19/T23).

Использование:
    python scripts/check_signals.py            # человекочитаемый вывод
    python scripts/check_signals.py --json     # JSON для мониторинга

Коды возврата: 0 — сигналов нет; 1 — есть сигналы; 2 — ошибка конфигурации
или БД. Проверяются сигналы, выводимые из состояния БД и файлов RUN_DIR:
возраст старейшего AI-задания, доля degraded-отчётов, heartbeat worker,
статус backup, остаток суточного бюджета. Сигнал 401/403 AI оценивается
монитором внутри worker-процесса (счётчики процесса) и ERROR-логом адаптера;
здесь он не дублируется.

Канал уведомлений оператору задаётся перед запуском (§3.10): скрипт
предназначен для cron/ручных проверок и интеграции с внешним мониторингом.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def _collect() -> tuple[dict, list[dict]]:
    from app.config import load_config
    from app.services import monitoring
    from app.storage.db import try_connect

    config = load_config()
    db = await try_connect(config.env.database_url)
    if db is None:
        raise RuntimeError("database unavailable")
    try:
        async with db.pool.acquire() as conn:
            gauges = await monitoring.collect_gauges(conn, config)
        signals = monitoring.evaluate_signals(
            gauges, config, monitoring.SignalState(), config_errors_total=0
        )
        # 401/403 в однократном внешнем процессе не виден: не сообщаем ложно
        signals = [s for s in signals if s["signal"] != "ai_auth_error_401_403"]
        return gauges, signals
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args()
    try:
        gauges, signals = asyncio.run(_collect())
    except Exception as exc:  # ошибка конфигурации/БД
        print(f"check_signals: error: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"gauges": gauges, "signals": signals},
                         ensure_ascii=False, default=str))
    else:
        for name in sorted(gauges):
            print(f"{name}: {gauges[name]}")
        if signals:
            print("\nSIGNALS:")
            for signal in signals:
                print(" ", json.dumps(signal, ensure_ascii=False))
        else:
            print("\nno operator signals")
    return 1 if signals else 0


if __name__ == "__main__":
    raise SystemExit(main())
