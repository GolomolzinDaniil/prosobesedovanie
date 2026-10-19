"""Общие фикстуры тестов части 1.

Интеграционные тесты требуют PostgreSQL: TEST_DATABASE_URL (по умолчанию
локальная prosob_test). База с именем, не содержащим 'test', не сбрасывается —
интеграционные тесты в этом случае пропускаются (защита от удаления данных).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TEST_DB_URL = "postgresql://app:app_dev_password@127.0.0.1:5432/prosob_test"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)

TABLES = [
    "ai_calls",
    "credits",
    "daily_usage",
    "deletion_tombstones",
    "events",
    "inbox",
    "jobs",
    "outbox",
    "payments",
    "reports",
    "sessions",
    "turns",
    "ui_tokens",
    "users",
]


def make_env(**overrides: str) -> dict[str, str]:
    env = {
        "BOT_TOKEN": "123456:TEST-abcdef-token",
        "WEBHOOK_SECRET": "s3cr3t-webhook-value-0001",
        "WEBHOOK_ROUTE_TOKEN": "route-token-random-000001",
        "DATABASE_URL": TEST_DATABASE_URL,
        "SETTINGS_PATH": str(ROOT / "config" / "settings.json"),
        "QUESTIONS_PATH": str(ROOT / "config" / "questions.json"),
        "PRODUCTS_PATH": str(ROOT / "config" / "products.json"),
        "PROVIDER_PROFILE_PATH": str(ROOT / "config" / "provider-profile.json"),
        "PROMPTS_DIR": str(ROOT / "prompts"),
        "MEDIA_DIR": str(ROOT / ".test-media"),
        "ENV_NAME": "dev",
        "LOG_LEVEL": "WARNING",
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Реестр метрик глобален: каждый тест начинает с чистых счётчиков."""
    from app.services import metrics

    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def settings_raw() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog_raw() -> dict[str, Any]:
    return json.loads((ROOT / "config" / "questions.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# База данных (интеграционные тесты)
# ---------------------------------------------------------------------------


def _safe_to_reset(dsn: str) -> bool:
    # Сброс схемы разрешён только для тестовой базы
    dbname = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    return "test" in dbname.lower()


@pytest.fixture(scope="session")
def db_url() -> str:
    """Готовит тестовую БД: чистая схема + применённые миграции (один раз)."""
    from app.storage.migrate import run_migrations

    async def _prepare() -> None:
        conn = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            if _safe_to_reset(TEST_DATABASE_URL):
                await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()
        await run_migrations(TEST_DATABASE_URL)

    try:
        asyncio.run(_prepare())
    except Exception as exc:  # БД недоступна — интеграционные тесты пропускаются
        pytest.skip(f"test database unavailable: {exc}")
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def db(db_url: str):
    """Соединение с очисткой таблиц до и после теста."""
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        yield conn
        await conn.execute(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def api_client(db_url: str, db):
    """API-приложение на тестовой БД + HTTP-клиент (lifespan запущен)."""
    from app.api.main import create_app

    env = make_env(DATABASE_URL=db_url)
    app = create_app(env)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, app


def write_filled_provider_profile(directory) -> str:
    """Рабочая копия provider-profile.json с заполненными значениями
    (тестовый провайдер; реальные ключи — из окружения)."""
    import json

    profile = json.loads(
        (ROOT / "config" / "provider-profile.example.json").read_text(encoding="utf-8")
    )
    for block in ("llm", "stt", "tts"):
        profile[block].update(
            {
                "provider": "test-provider",
                "model": "test-model",
                "endpoint": "https://api.test/v1",
                "region": "ru",
                "data_retention_terms": "не хранит запросы",
            }
        )
    profile["llm"]["context_window_tokens"] = 32000
    profile["llm"]["temperature_supported"] = True
    profile["llm"]["native_json_schema_supported"] = True
    profile["llm"]["smoke"] = {
        "json_request_checked_at": "2026-10-18T00:00:00Z",
        "context_window_confirmed_min_16000": True,
    }
    profile["stt"]["smoke"] = {"russian_ogg_opus_checked_at": "2026-10-18T00:00:00Z"}
    profile["tts"]["smoke"] = {"russian_ogg_opus_checked_at": "2026-10-18T00:00:00Z"}
    path = directory / "provider-profile.json"
    path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest_asyncio.fixture
async def harness(db_url: str, db, tmp_path_factory):
    """Worker-конвейер с фейковыми ботом и LLM: inbox -> домен -> jobs -> outbox.

    Тестовые допущения: лимит команд поднят до 1000/мин (продуктовый лимит
    проверяет отдельный тест); паузы повторов провайдера обнулены; профиль
    провайдера заполнен тестовыми значениями, вызовы идут в FakeLlm.
    """
    import json

    from tests.fakes import Harness

    cfg_dir = tmp_path_factory.mktemp("cfg")
    settings = json.loads(
        (ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    settings["limits"]["commands_per_minute"] = 1000
    settings["limits"]["provider_retry_pause_min_seconds"] = 0
    settings["limits"]["provider_retry_pause_max_seconds"] = 0
    custom = cfg_dir / "settings.json"
    custom.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    profile_path = write_filled_provider_profile(cfg_dir)

    env = make_env(
        DATABASE_URL=db_url,
        SETTINGS_PATH=str(custom),
        PROVIDER_PROFILE_PATH=profile_path,
        LLM_API_KEY="test-key",
        STT_API_KEY="test-key",
        TTS_API_KEY="test-key",
        RUN_DIR=str(cfg_dir / "run"),
    )
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    try:
        yield Harness(pool, env)
    finally:
        await pool.close()


WEBHOOK_PATH = "/telegram/webhook/route-token-random-000001"
WEBHOOK_HEADERS = {
    "X-Telegram-Bot-Api-Secret-Token": "s3cr3t-webhook-value-0001",
    "Content-Type": "application/json",
}


def make_update(
    update_id: int = 1001,
    *,
    chat_id: int = 555,
    text: str = "/start",
    message_id: int = 1,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 1760000000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
        "text": text,
    }
    if extra:
        message.update(extra)
    return {"update_id": update_id, "message": message}


def make_callback_update(
    update_id: int = 2001, data: str = "select_role:abcdefgh"
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 555, "is_bot": False, "first_name": "Test"},
            "chat_instance": "-1000000000000",
            "data": data,
        },
    }
