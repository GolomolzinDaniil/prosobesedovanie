"""Метрики процесса (§3.10; задача T19).

Счётчики и задержки этапов без содержимого ответов, расшифровок и
AI-payload: только имена, метки-категории и числа. Реестр lebt в процессе
worker; периодический снимок пишется структурированным логом `metrics`,
сигналы оператору вычисляет monitoring.py.

Покрыты пункты §3.10: обработка inbox, lease-expiry, задержка каждого этапа,
число API-повторов, 429/5xx, доля невалидного JSON, fallback/degraded,
стоимость, успех report-delivery, STT-подтверждения/перезаписи.
"""

from __future__ import annotations

import threading
import time
from typing import Any


def _key(name: str, labels: dict[str, str] | None) -> str:
    if not labels:
        return name
    ordered = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{ordered}}}"


class MetricsRegistry:
    """Потокобезопасный реестр счётчиков и наблюдений задержек."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._observations: dict[str, list[float]] = {}

    def inc(self, name: str, value: int = 1, **labels: str) -> None:
        key = _key(name, labels or None)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        key = _key(name, labels or None)
        with self._lock:
            bucket = self._observations.setdefault(key, [])
            bucket.append(seconds)
            # резервуар: не более 1024 последних наблюдений на метрику
            if len(bucket) > 1024:
                del bucket[: len(bucket) - 1024]

    def counter(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters.get(_key(name, labels or None), 0)

    def snapshot(self) -> dict[str, Any]:
        """Счётчики и p50/p95 задержек; только числа и метки-категории."""
        with self._lock:
            counters = dict(self._counters)
            observations = {k: list(v) for k, v in self._observations.items()}
        latencies: dict[str, dict[str, float]] = {}
        for key, values in observations.items():
            if not values:
                continue
            ordered = sorted(values)
            latencies[key] = {
                "count": len(ordered),
                "p50": round(_percentile(ordered, 50), 4),
                "p95": round(_percentile(ordered, 95), 4),
            }
        return {"counters": counters, "latency": latencies}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._observations.clear()


def _percentile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


REGISTRY = MetricsRegistry()


class StageTimer:
    """Контекстный таймер этапа: observe(name, elapsed, **labels)."""

    def __init__(self, name: str, **labels: str) -> None:
        self.name = name
        self.labels = labels
        self.started = 0.0

    def __enter__(self) -> StageTimer:
        self.started = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        REGISTRY.observe(self.name, time.monotonic() - self.started, **self.labels)


# Имена метрик (единый словарь; значения меток — категории без контента)
INBOX_PROCESSED = "inbox_processed_total"          # status=processed|skipped|failed|requeued
INBOX_LEASE_EXPIRED = "inbox_lease_expired_total"
JOB_PROCESSED = "job_processed_total"              # kind, state=done|failed|cancelled
JOB_LEASE_EXPIRED = "job_lease_expired_total"
PROVIDER_CALLS = "provider_calls_total"            # operation, outcome
PROVIDER_RETRIES = "provider_retries_total"        # operation
OUTBOX_SENT = "outbox_sent_total"                  # kind, outcome
FALLBACK = "fallback_total"                        # operation, reason
REPORT_DEGRADED = "report_degraded_total"
REPORT_DELIVERED = "report_delivered_total"        # outcome=success|failed
STT_ACTION = "stt_action_total"                    # action=confirmed|edited|rerecorded
COST_RESERVED_USD = "cost_reserved_usd_micros"     # микродоллары: целые счётчики
COST_ACTUAL_USD = "cost_actual_usd_micros"
COST_UNKNOWN_USD = "cost_unknown_usd_micros"
LATENCY_INBOX = "latency_inbox_seconds"
LATENCY_JOB = "latency_job_seconds"                # kind
LATENCY_OUTBOX = "latency_outbox_seconds"
LATENCY_PROVIDER = "latency_provider_seconds"      # operation

# Исходы provider-вызовов (outcome у PROVIDER_CALLS)
OUTCOME_SUCCESS = "success"
OUTCOME_RATE_LIMITED = "rate_limited"              # 429
OUTCOME_SERVER_ERROR = "server_error"              # 5xx/сеть (retryable)
OUTCOME_INVALID = "invalid_response"               # 400: фатально
OUTCOME_CONFIG_ERROR = "config_error"              # 401/403: сигнал оператору
OUTCOME_INVALID_JSON = "invalid_json"              # невалидный JSON ответа модели
OUTCOME_UNKNOWN = "unknown"                        # неизвестный результат


def micros(usd: float | None) -> int:
    """Стоимость в целых микродолларах для счётчиков (без float-дрейфа)."""
    if usd is None:
        return 0
    return round(float(usd) * 1_000_000)


def record_call(operation: str, outcome: str, started_monotonic: float) -> None:
    REGISTRY.inc(PROVIDER_CALLS, operation=operation, outcome=outcome)
    REGISTRY.observe(
        LATENCY_PROVIDER, time.monotonic() - started_monotonic, operation=operation
    )


def outcome_for_error(exc: Exception) -> str:
    """Классификация ошибки провайдера по типу (§3.2): единый словарь
    исходов для LLM/STT/TTS на границе порта."""
    name = type(exc).__name__
    if name.endswith("RetryableError"):
        return (
            OUTCOME_RATE_LIMITED
            if getattr(exc, "status", None) == 429
            else OUTCOME_SERVER_ERROR
        )
    if name.endswith("ConfigError"):
        return OUTCOME_CONFIG_ERROR
    if name.endswith("FatalError"):
        return OUTCOME_INVALID
    # UnknownResultError, сетевые таймауты — неизвестный исход
    return OUTCOME_UNKNOWN


def provider_instrumented(operation: str):
    """Декоратор вызова провайдера: классифицирует исход по типу ошибки
    (Retryable/Config/UnknownResult/Fatal) и учитывает попытку и задержку."""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                name = type(exc).__name__
                if name.endswith("RetryableError"):
                    outcome = (
                        OUTCOME_RATE_LIMITED
                        if getattr(exc, "status", None) == 429
                        else OUTCOME_SERVER_ERROR
                    )
                elif name.endswith("ConfigError"):
                    outcome = OUTCOME_CONFIG_ERROR
                elif name.endswith("UnknownResultError"):
                    outcome = OUTCOME_UNKNOWN
                elif name.endswith("FatalError"):
                    outcome = OUTCOME_INVALID
                else:
                    raise
                record_call(operation, outcome, started)
                raise
            record_call(operation, OUTCOME_SUCCESS, started)
            return result

        return wrapper

    return decorator
