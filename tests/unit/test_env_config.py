"""Проверка секретов и переменных окружения при запуске (§3.6, §3.9, §3.10)."""

from __future__ import annotations

import pytest

from app.config import ConfigError, EnvConfig, load_config, load_prompts
from tests.conftest import ROOT, make_env


def test_env_from_minimal_set() -> None:
    env = EnvConfig.from_environ(make_env())
    assert env.bot_token
    assert env.env_name == "dev"
    assert env.migrations_database_url == env.database_url


def test_missing_secret_blocks_startup() -> None:
    for missing in ("BOT_TOKEN", "WEBHOOK_SECRET", "WEBHOOK_ROUTE_TOKEN", "DATABASE_URL"):
        env = make_env()
        env.pop(missing)
        with pytest.raises(ConfigError) as exc_info:
            EnvConfig.from_environ(env)
        assert missing in exc_info.value.problems[0]


def test_route_token_must_not_contain_bot_token() -> None:
    env = make_env(WEBHOOK_ROUTE_TOKEN="prefix-123456:TEST-abcdef-token")
    with pytest.raises(ConfigError) as exc_info:
        EnvConfig.from_environ(env)
    assert any("не должен содержать токен бота" in p for p in exc_info.value.problems)


def test_short_secret_rejected() -> None:
    env = make_env(WEBHOOK_SECRET="short")
    with pytest.raises(ConfigError) as exc_info:
        EnvConfig.from_environ(env)
    assert any("16..255" in p for p in exc_info.value.problems)


def test_non_ascii_secret_rejected() -> None:
    env = make_env(WEBHOOK_SECRET="секрет-вебхук-значение-01")
    with pytest.raises(ConfigError) as exc_info:
        EnvConfig.from_environ(env)
    assert any("ASCII" in p for p in exc_info.value.problems)


def test_unknown_env_name_rejected() -> None:
    env = make_env(ENV_NAME="prod-local")
    with pytest.raises(ConfigError):
        EnvConfig.from_environ(env)


def test_load_config_fails_fast_on_broken_settings(tmp_path) -> None:
    import json

    broken = tmp_path / "settings.json"
    data = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    data["interview"]["max_turns"] = 7  # нарушение инварианта §3.6
    broken.write_text(json.dumps(data), encoding="utf-8")
    env = make_env(SETTINGS_PATH=str(broken))
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert any("max_turns" in p for p in exc_info.value.problems)


def test_load_config_missing_file() -> None:
    env = make_env(SETTINGS_PATH="/nonexistent/settings.json")
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert any("не найден" in p for p in exc_info.value.problems)


def test_prompts_are_versioned_files() -> None:
    prompts = load_prompts(ROOT / "prompts")
    assert set(prompts) == {"turn-system.txt", "report-system.txt", "user-envelope.txt"}
    for name, text in prompts.items():
        assert "2.0" in text.splitlines()[0], name


def test_missing_prompt_blocks_startup(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_prompts(tmp_path)


def test_full_config_loads_and_ai_blocked_without_profile() -> None:
    """Незаполненные внешние зависимости блокируют AI-вызовы, не webhook (§1.4)."""
    config = load_config(make_env())
    assert config.ai_enabled is False
    assert any("AI-вызовы заблокированы" in w for w in config.warnings)
    # промпты и каталог загружены
    assert len(config.prompts) == 3
    assert sum(len(s.entries) for s in config.catalog.scenarios) == 36


def test_full_config_ai_enabled_with_filled_profile(tmp_path) -> None:
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
    # smoke-проверки выполнены (T01, §1.4/§5.3)
    profile["llm"]["smoke"] = {
        "json_request_checked_at": "2026-10-18T00:00:00Z",
        "context_window_confirmed_min_16000": True,
    }
    profile["stt"]["smoke"] = {"russian_ogg_opus_checked_at": "2026-10-18T00:00:00Z"}
    profile["tts"]["smoke"] = {"russian_ogg_opus_checked_at": "2026-10-18T00:00:00Z"}
    profile_path = tmp_path / "provider-profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    env = make_env(
        PROVIDER_PROFILE_PATH=str(profile_path),
        LLM_API_KEY="test-key",
        STT_API_KEY="test-key",
        TTS_API_KEY="test-key",
    )
    config = load_config(env)
    assert config.ai_enabled is True
    assert config.ai_missing == []


def test_profile_with_nulls_blocks_ai(tmp_path) -> None:
    """Значения null означают незаполненную внешнюю зависимость (§1.4)."""
    import shutil

    profile_path = tmp_path / "provider-profile.json"
    shutil.copy(ROOT / "config" / "provider-profile.example.json", profile_path)
    env = make_env(PROVIDER_PROFILE_PATH=str(profile_path))
    config = load_config(env)
    assert config.ai_enabled is False
    assert any("llm.provider" in m for m in config.ai_missing)
