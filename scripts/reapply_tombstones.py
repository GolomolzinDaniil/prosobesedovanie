"""Повторное применение удалений по tombstones после restore (§3.9, AT13).

Обязательный шаг восстановления: «Во время restore принять трафик только
после повторного удаления». Скрипт выполняется ДО запуска api/worker:
1. восстановленный inbox удалённых профилей признаётся skipped
   (сверяется с tombstones, пока user_id не обнулён);
2. профили, совпадающие с tombstone, удаляются повторно вместе с данными.

Использование (в контейнере с .env):
    python scripts/reapply_tombstones.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def _run() -> int:
    import asyncpg

    from app.config import ConfigError, load_config
    from app.services import lifecycle

    try:
        config = load_config()
    except ConfigError as exc:
        for problem in exc.problems:
            print(f"config: {problem}", file=sys.stderr)
        return 2
    try:
        conn = await asyncpg.connect(config.env.database_url)
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"database unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        inbox_skipped = await lifecycle.purge_inbox_of_deleted_users(conn)
        re_deleted = await lifecycle.reapply_tombstones(conn)
        tombstones = await conn.fetchval("SELECT count(*) FROM deletion_tombstones")
    finally:
        await conn.close()
    print(
        f"reapply_tombstones: inbox_skipped={inbox_skipped} "
        f"profiles_re_deleted={re_deleted} tombstones_active={tombstones}"
    )
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
