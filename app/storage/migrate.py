"""Применение SQL-миграций с контролем неизменяемости (§3.10, задача T03).

- файлы migrations/*.sql применяются в лексикографическом порядке;
- каждая миграция — в отдельной транзакции;
- контрольная сумма SHA-256 фиксируется: изменение уже применённой миграции
  блокирует запуск (создавайте новую версию);
- CLI: python -m app.storage.migrate [DSN] (по умолчанию MIGRATIONS_DATABASE_URL
  или DATABASE_URL — роль миграций отделена от роли приложения, §3.9).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(Exception):
    pass


def _discover(migrations_dir: Path) -> list[Path]:
    files = sorted(p for p in migrations_dir.glob("*.sql"))
    if not files:
        raise MigrationError(f"в {migrations_dir} нет миграций *.sql")
    return files


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run_migrations(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Применяет недостающие миграции. Возвращает список применённых имён."""
    files = _discover(migrations_dir)
    applied_now: list[str] = []
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_ENSURE_TABLE)
        rows = await conn.fetch("SELECT filename, checksum FROM schema_migrations")
        recorded = {r["filename"]: r["checksum"] for r in rows}

        for path in files:
            digest = _checksum(path)
            if path.name in recorded:
                if recorded[path.name] != digest:
                    raise MigrationError(
                        f"миграция {path.name} изменена после применения "
                        f"(ожидалась сумма {recorded[path.name][:12]}…, "
                        f"получена {digest[:12]}…); создайте новую миграцию"
                    )
                continue
            sql = path.read_text(encoding="utf-8")
            logger.info("applying migration %s", path.name)
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    path.name,
                    digest,
                )
            except Exception:
                await tx.rollback()
                raise
            await tx.commit()
            applied_now.append(path.name)
        return applied_now
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    dsn = (
        sys.argv[1]
        if len(sys.argv) > 1
        else (os.environ.get("MIGRATIONS_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    )
    if not dsn:
        print("Укажите DSN аргументом либо MIGRATIONS_DATABASE_URL/DATABASE_URL", file=sys.stderr)
        raise SystemExit(2)
    applied = asyncio.run(run_migrations(dsn))
    print(f"Применено миграций: {len(applied)}" + (f" -> {', '.join(applied)}" if applied else ""))


if __name__ == "__main__":
    main()
