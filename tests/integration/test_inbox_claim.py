"""Механизм надёжной выборки inbox (§2.1): SKIP LOCKED, lease, повторы.

Проверяет транзакционный механизм, на котором в части 2 строится обработчик:
конкурирующие потребители не пересекаются, просроченный lease возвращается
в очередь, обработчик повторяется до 5 раз, дубликаты не создают повторных
доменных эффектов.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg

from app.storage import inbox_repo
from tests.conftest import make_callback_update, make_update


async def _seed(db: asyncpg.Connection, update_ids: list[int]) -> None:
    for uid in update_ids:
        await inbox_repo.insert_update(
            db, update_id=uid, kind="message", payload=make_update(uid)
        )


async def test_insert_dedup_returns_false(db: asyncpg.Connection) -> None:
    first = await inbox_repo.insert_update(
        db, update_id=7001, kind="message", payload=make_update(7001)
    )
    second = await inbox_repo.insert_update(
        db, update_id=7001, kind="message", payload=make_update(7001)
    )
    assert first is True
    assert second is False
    assert await db.fetchval("SELECT count(*) FROM inbox WHERE update_id = 7001") == 1


async def test_claim_skips_locked_rows_between_consumers(
    db_url: str, db: asyncpg.Connection
) -> None:
    """Два конкурирующих потребителя не получают одни и те же строки (§2.1)."""
    await _seed(db, list(range(7100, 7120)))

    conn_a = await asyncpg.connect(db_url)
    conn_b = await asyncpg.connect(db_url)
    try:
        tx_a = conn_a.transaction()
        await tx_a.start()
        batch_a = await inbox_repo.claim_batch(conn_a, owner="worker-a", limit=10)

        # пока транзакция A открыта, B видит только незаблокированные строки
        batch_b = await inbox_repo.claim_batch(conn_b, owner="worker-b", limit=10)

        ids_a = {r["update_id"] for r in batch_a}
        ids_b = {r["update_id"] for r in batch_b}
        assert len(ids_a) == 10
        assert len(ids_b) == 10
        assert ids_a.isdisjoint(ids_b)
        for record in batch_a:
            assert record["status"] == "processing"
            assert record["lease_owner"] == "worker-a"
            assert record["attempts"] == 1
        await tx_a.rollback()
    finally:
        await conn_a.close()
        await conn_b.close()


async def test_concurrent_claims_do_not_overlap(db_url: str, db: asyncpg.Connection) -> None:
    """Параллельная выборка из 5 потребителей: пересечений нет, всё разобрано."""
    await _seed(db, list(range(7200, 7250)))

    async def worker(name: str) -> set[int]:
        conn = await asyncpg.connect(db_url)
        claimed: set[int] = set()
        try:
            while True:
                batch = await inbox_repo.claim_batch(conn, owner=name, limit=5)
                if not batch:
                    break
                for record in batch:
                    claimed.add(record["update_id"])
                    await inbox_repo.mark_processed(conn, record["update_id"])
        finally:
            await conn.close()
        return claimed

    results = await asyncio.gather(*(worker(f"w{i}") for i in range(5)))
    all_claimed = [uid for chunk in results for uid in chunk]
    assert len(all_claimed) == len(set(all_claimed)) == 50
    remaining = await db.fetchval(
        "SELECT count(*) FROM inbox WHERE update_id BETWEEN 7200 AND 7249 AND status <> 'processed'"
    )
    assert remaining == 0


async def test_expired_lease_returns_to_queue(db: asyncpg.Connection) -> None:
    await _seed(db, [7300])
    now = datetime.now(UTC) - timedelta(seconds=100)
    batch = await inbox_repo.claim_batch(
        db, owner="worker-crashed", limit=10, lease_seconds=120, now=now
    )
    assert [r["update_id"] for r in batch] == [7300]

    # lease ещё не истёк — другой worker строку не забирает
    assert (
        await inbox_repo.claim_batch(
            db, owner="worker-new", limit=10, now=now + timedelta(seconds=60)
        )
        == []
    )

    # возврат просроченных lease (восстановление после перезапуска, §3.2)
    future = now + timedelta(seconds=200)
    released = await inbox_repo.release_expired_leases(db, now=future)
    assert released == 1
    batch2 = await inbox_repo.claim_batch(db, owner="worker-new", limit=10, now=future)
    assert [r["update_id"] for r in batch2] == [7300]
    # номер попытки увеличивается
    assert batch2[0]["attempts"] == 2


async def test_requeue_backoff_and_max_retries(db: asyncpg.Connection) -> None:
    await _seed(db, [7400])
    now = datetime.now(UTC)
    await inbox_repo.claim_batch(db, owner="w", limit=1, now=now)

    # повтор с backoff: строка не видна до наступления next_attempt_at
    status = await inbox_repo.requeue(
        db, 7400, error="boom", backoff_seconds=60, max_retries=5, now=now
    )
    assert status == "pending"
    assert await inbox_repo.claim_batch(db, owner="w", limit=10, now=now) == []
    later = now + timedelta(seconds=61)
    batch = await inbox_repo.claim_batch(db, owner="w", limit=10, now=later)
    assert [r["update_id"] for r in batch] == [7400]

    # исчерпание повторов (max 5, §3.2): attempts уже 2 -> доводим до 5
    for _ in range(3):
        await inbox_repo.requeue(
            db, 7400, error="boom", backoff_seconds=1, max_retries=5, now=later
        )
        claimed = await inbox_repo.claim_batch(
            db, owner="w", limit=1, now=later + timedelta(seconds=5)
        )
        assert claimed, "claim обязан забрать строку до исчерпания повторов"
    # пятая попытка выполнена; повторный отказ переводит строку в failed
    status = await inbox_repo.requeue(
        db, 7400, error="boom", backoff_seconds=1, max_retries=5, now=later
    )
    assert status == "failed"
    row = await inbox_repo.get_update(db, 7400)
    assert row["status"] == "failed"
    assert row["attempts"] == 5
    assert row["processed_at"] is not None
    assert row["last_error"] == "boom"


async def test_mark_processed_and_payload_purge(db: asyncpg.Connection) -> None:
    await _seed(db, [7500])
    await inbox_repo.claim_batch(db, owner="w", limit=1)
    await inbox_repo.mark_processed(db, 7500, purge_payload=True)
    row = await inbox_repo.get_update(db, 7500)
    assert row["status"] == "processed"
    assert row["payload"] is None  # payload удаляется после обработки (§3.9)
    assert row["payload_purged_at"] is not None
    assert row["lease_owner"] is None
    # обработанная строка больше не забирается
    assert await inbox_repo.claim_batch(db, owner="w2", limit=10) == []


async def test_mark_skipped(db: asyncpg.Connection) -> None:
    await inbox_repo.claim_batch(db, owner="w", limit=1)  # пусто
    await db.execute(
        "INSERT INTO inbox (update_id, kind, status, payload) "
        "VALUES (7600, 'unsupported:poll', 'pending', '{}'::jsonb)"
    )
    await inbox_repo.mark_skipped(db, 7600)
    row = await inbox_repo.get_update(db, 7600)
    assert row["status"] == "skipped"
    assert row["processed_at"] is not None


async def test_pending_stats(db: asyncpg.Connection) -> None:
    await _seed(db, [7700, 7701])
    await inbox_repo.insert_update(
        db,
        update_id=7702,
        kind="unsupported:poll",
        payload={"update_id": 7702, "poll": {}},
        initial_status="skipped",
    )
    await inbox_repo.claim_batch(db, owner="w", limit=1)
    stats = await inbox_repo.pending_stats(db)
    assert stats.get("pending", 0) + stats.get("processing", 0) == 2
    assert stats.get("skipped") == 1


async def test_callback_payload_preserved_raw(db: asyncpg.Connection) -> None:
    update = make_callback_update(7800, data="start_session:tokentokentoken12")
    await inbox_repo.insert_update(
        db, update_id=7800, kind="callback_query", payload=update
    )
    row = await inbox_repo.get_update(db, 7800)
    import json

    assert json.loads(row["payload"]) == update
