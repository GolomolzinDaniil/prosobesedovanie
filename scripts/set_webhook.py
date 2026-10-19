"""Установка webhook с секретом и разрешёнными видами update (§3.10, T23).

Шаг стартового развёртывания: «установить webhook с секретом и разрешёнными
видами update». URL собирается из PUBLIC_BASE_URL и WEBHOOK_ROUTE_TOKEN
(публичный HTTPS обязателен — reverse proxy с TLS перед API).

Использование (в контейнере api или на хосте с .env):
    python scripts/set_webhook.py            # установить и проверить
    python scripts/set_webhook.py --delete   # снять webhook
    python scripts/set_webhook.py --info     # только getWebhookInfo
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Разрешённые виды update (§3.1): только обслуживаемые ботом.
# pre_checkout_query добавляется при включённом коммерческом этапе (P01–P06).
ALLOWED_UPDATES = ["message", "callback_query"]


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        _api_url(token, method),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    from app.config import ConfigError, load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="deleteWebhook")
    parser.add_argument("--info", action="store_true", help="getWebhookInfo")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"config: {problem}", file=sys.stderr)
        return 2

    token = config.env.bot_token
    if args.delete:
        result = _call(token, "deleteWebhook", {"drop_pending_updates": False})
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.info:
        result = _call(token, "getWebhookInfo")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    base = (config.env.public_base_url or "").rstrip("/")
    if not base:
        print("PUBLIC_BASE_URL не задан: webhook не установить (§3.10)", file=sys.stderr)
        return 2
    if not base.startswith("https://"):
        print("PUBLIC_BASE_URL обязан быть HTTPS (TLS обязателен, §3.9)", file=sys.stderr)
        return 2

    url = f"{base}/telegram/webhook/{config.env.webhook_route_token}"
    allowed = list(ALLOWED_UPDATES)
    if config.settings.flags.billing_enabled:
        allowed.append("pre_checkout_query")
    body = {
        "url": url,
        "secret_token": config.env.webhook_secret,
        "allowed_updates": allowed,
        "max_connections": 40,
    }
    result = _call(token, "setWebhook", body)
    if not result.get("ok"):
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    # Проверка: getWebhookInfo без раскрытия секрета
    info = _call(token, "getWebhookInfo")
    data = info.get("result", {})
    print(
        json.dumps(
            {
                "ok": info.get("ok"),
                "url": data.get("url"),
                "pending_update_count": data.get("pending_update_count", 0),
                "last_error": data.get("last_error_message"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if data.get("url") == url else 1


if __name__ == "__main__":
    raise SystemExit(main())
