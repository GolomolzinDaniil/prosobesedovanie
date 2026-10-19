"""Метрики (§3.10; T19): счётчики, задержки, классификация исходов.

Метрики не содержат ответа, расшифровки и AI-payload — только имена,
метки-категории и числа (§3.9).
"""

from __future__ import annotations

import pytest

from app.services import metrics


@pytest.fixture(autouse=True)
def _clean_registry():
    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


def test_counter_increment_and_labels() -> None:
    metrics.REGISTRY.inc("c_total")
    metrics.REGISTRY.inc("c_total")
    metrics.REGISTRY.inc("c_total", outcome="success")
    assert metrics.REGISTRY.counter("c_total") == 2
    assert metrics.REGISTRY.counter("c_total", outcome="success") == 1


def test_counter_value_step() -> None:
    metrics.REGISTRY.inc("bytes_total", 500)
    metrics.REGISTRY.inc("bytes_total", 250)
    assert metrics.REGISTRY.counter("bytes_total") == 750


def test_snapshot_shape_and_percentiles() -> None:
    for value in (1.0, 2.0, 3.0, 4.0, 10.0):
        metrics.REGISTRY.observe("lat", value, kind="assess")
    snap = metrics.REGISTRY.snapshot()
    assert "counters" in snap and "latency" in snap
    lat = snap["latency"]["lat{kind=assess}"]
    assert lat["count"] == 5
    assert lat["p50"] == 3.0
    # p95 между 4 и 10 (линейная интерполяция)
    assert 4.0 <= lat["p95"] <= 10.0


def test_snapshot_has_no_content_fields() -> None:
    """В снимок попадают только имена/метки/числа, не содержимое."""
    metrics.REGISTRY.inc(metrics.PROVIDER_CALLS, operation="assess",
                         outcome=metrics.OUTCOME_SUCCESS)
    snap = metrics.REGISTRY.snapshot()
    blob = repr(snap)
    # метки — категории из фиксированного словаря
    assert "operation=assess" in blob
    assert "outcome=success" in blob


def test_observation_reservoir_bounded() -> None:
    for i in range(2000):
        metrics.REGISTRY.observe("bounded", float(i))
    snap = metrics.REGISTRY.snapshot()
    assert snap["latency"]["bounded"]["count"] <= 1024


def test_stage_timer_observes() -> None:
    with metrics.StageTimer("stage", kind="report"):
        pass
    snap = metrics.REGISTRY.snapshot()
    assert "stage{kind=report}" in snap["latency"]


def test_micros_conversion() -> None:
    assert metrics.micros(None) == 0
    assert metrics.micros(0.000001) == 1
    assert metrics.micros(1.5) == 1_500_000


def test_provider_instrumented_success() -> None:
    from app.providers.stt import HttpSttClient  # noqa: F401  (импорт без side-effect)

    class FakeClient:
        @metrics.provider_instrumented("stt")
        async def transcribe(self, path: str) -> str:
            return "ok"

    import asyncio

    client = FakeClient()
    assert asyncio.run(client.transcribe("/tmp/x.ogg")) == "ok"
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_SUCCESS
    ) == 1


def test_provider_instrumented_classifies_errors() -> None:
    import asyncio

    from app.providers.stt import (
        SttConfigError,
        SttFatalError,
        SttRetryableError,
        SttUnknownResultError,
    )

    def make(exc):
        class C:
            @metrics.provider_instrumented("stt")
            async def transcribe(self, path: str) -> str:
                raise exc

        return C()

    async def run_all():
        for exc, _ in (
            (SttRetryableError(status=429), metrics.OUTCOME_RATE_LIMITED),
            (SttRetryableError(status=503), metrics.OUTCOME_SERVER_ERROR),
            (SttConfigError(status=401), metrics.OUTCOME_CONFIG_ERROR),
            (SttFatalError(status=400), metrics.OUTCOME_INVALID),
            (SttUnknownResultError("net"), metrics.OUTCOME_UNKNOWN),
        ):
            with pytest.raises(type(exc)):
                await make(exc).transcribe("/tmp/x.ogg")

    asyncio.run(run_all())
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_RATE_LIMITED
    ) == 1
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_SERVER_ERROR
    ) == 1
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_CONFIG_ERROR
    ) == 1
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_INVALID
    ) == 1
    assert metrics.REGISTRY.counter(
        metrics.PROVIDER_CALLS, operation="stt", outcome=metrics.OUTCOME_UNKNOWN
    ) == 1


def test_provider_instrumented_ignores_foreign_errors() -> None:
    import asyncio

    class C:
        @metrics.provider_instrumented("tts")
        async def synthesize(self, text: str) -> float:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(C().synthesize("привет"))
    # неклассифицированная ошибка не учитывается как исход провайдера
    snap = metrics.REGISTRY.snapshot()
    assert not any(k.startswith(metrics.PROVIDER_CALLS) for k in snap["counters"])
