"""Голосовой режим (§3.5; задачи T15, T16, T17).

Transcribe-задание: скачивание только через Telegram getFile с жёстким
пределом 10 MiB (поток ограничивается до полного скачивания), проверка
OGG/Opus и фактической длительности изолированным декодером, STT-вызов
(≤2 на операцию, дедлайн 150 с), черновик расшифровки с transcript_version
и экран подтверждения. Пустой STT или результат длиннее 4000 символов не
засчитывается. Аудио удаляется в finally; при аварии — sweep и startup
cleanup.

TTS-задание: озвучивается только текст вопроса (≤700 символов), без ответа
пользователя; результат — дополнение к доставленному текстовому вопросу
(reply на него), необязательная строка outbox; сбой озвучивания не блокирует
интервью («Озвучка временно недоступна; вопрос есть текстом»).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg

from app.config import AppConfig
from app.domain.answers import normalize_answer_text
from app.providers.stt import (
    SttConfigError,
    SttFatalError,
    SttRetryableError,
    SttUnknownResultError,
)
from app.providers.tts import (
    TtsConfigError,
    TtsFatalError,
    TtsRetryableError,
    TtsUnknownResultError,
)
from app.services import metrics, session_service, token_budget
from app.services.media_service import (
    MediaStorageFullError,
    check_capacity,
    create_private_file,
    new_media_path,
    probe_ogg_opus_async,
    remove_file,
)
from app.services.token_budget import estimate_tokens
from app.services.ui_tokens import generate_ui_token
from app.storage import jobs_repo, outbox_repo, sessions_repo, turns_repo, ui_tokens_repo
from app.telegram import messages
from app.telegram.bot import DownloadOutcome
from app.telegram.markup import buttons_payload, text_payload
from contracts.models import ANSWER_MAX_CHARS, ErrorCode, SessionStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transcribe (§3.5)
# ---------------------------------------------------------------------------


async def process_transcribe_job(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str
) -> None:
    config: AppConfig = deps.config
    settings = config.settings
    audio_path: Path | None = None
    failure: tuple[ErrorCode, str] | None = None
    draft: str | None = None
    started = time.monotonic()
    deadline = started + settings.timeouts_seconds.transcription_deadline_total

    try:
        async with pool.acquire() as conn:
            turn = await turns_repo.find_by_id(conn, job["turn_id"])
            session = (
                await sessions_repo.find_by_id(conn, job["session_id"])
                if turn is not None
                else None
            )
        if turn is None or session is None:
            await _mark(job, owner, pool, "failed", error_code="CONTEXT_GONE")
            return
        if session["status"] != SessionStatus.transcribing.value:
            await _mark(job, owner, pool, "done", result={"stale": True})
            return

        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        file_id = payload.get("file_id")

        # 1. Скачивание с жёстким пределом потока (§3.5)
        audio_path = new_media_path(config.env.media_dir)
        create_private_file(audio_path)
        await _set_media_key(pool, job, str(audio_path))
        try:
            check_capacity(
                config.env.media_dir,
                settings.limits.media_total_max_mib,
                incoming_bytes=settings.limits.voice_max_bytes,
            )
        except MediaStorageFullError:
            failure = (ErrorCode.MEDIA_STORAGE_FULL, "storage_full")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return

        result = await deps.bot.download_file(
            file_id, str(audio_path), settings.limits.voice_max_bytes
        )
        if result.outcome is DownloadOutcome.too_large:
            failure = (ErrorCode.VOICE_TOO_LARGE, "stream_too_large")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return
        if result.outcome is not DownloadOutcome.success:
            code = (
                ErrorCode.VOICE_TOO_LARGE
                if result.outcome is DownloadOutcome.retryable
                and (payload.get("file_size") or 0) > settings.limits.voice_max_bytes
                else ErrorCode.TRANSCRIPTION_FAILED
            )
            failure = (code, f"download:{result.outcome.value}")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return

        # 2. Изолированный декодер: фактический формат и длительность (§3.5)
        probe = await probe_ogg_opus_async(
            audio_path,
            wall_timeout=settings.timeouts_seconds.media_decode,
            memory_mib=settings.limits.decoder_memory_max_mib,
        )
        if not probe.ok:
            code = (
                ErrorCode.VOICE_FORMAT_UNSUPPORTED
                if probe.error in ("not_ogg_container", "not_opus_codec")
                else ErrorCode.TRANSCRIPTION_FAILED
            )
            failure = (code, f"probe:{probe.error}")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return
        if (probe.duration_seconds or 0) > settings.limits.voice_max_seconds:
            failure = (ErrorCode.VOICE_TOO_LONG, "probe_duration")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return

        # 3. STT-вызовы (≤2 на операцию, бюджет, дедлайн)
        if deps.stt is None or not config.ai_enabled:
            failure = (ErrorCode.TRANSCRIPTION_FAILED, "stt_disabled")
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return

        stt_text, failure = await _run_stt(
            pool, deps, job, str(audio_path), probe.duration_seconds or 0.0, deadline
        )
        if failure is not None:
            await _fail_to_awaiting(pool, deps, job, owner, turn, session, failure)
            return

        normalized = normalize_answer_text(stt_text or "")
        if not normalized:
            failure = (ErrorCode.TRANSCRIPTION_FAILED, "empty_transcript")
            await _fail_to_awaiting(
                pool, deps, job, owner, turn, session, failure,
                message_override=messages.TRANSCRIPT_EMPTY,
            )
            return
        if len(normalized) > ANSWER_MAX_CHARS:
            failure = (ErrorCode.TRANSCRIPTION_FAILED, "transcript_too_long")
            await _fail_to_awaiting(
                pool, deps, job, owner, turn, session, failure,
                message_override=messages.TRANSCRIPT_TOO_LONG,
            )
            return
        draft = normalized
    finally:
        # Аудио удаляется в finally (§3.5); производные — вместе с оригиналом
        remove_file(audio_path)
        if audio_path is not None:
            await _set_media_key(pool, job, None)

    await _save_draft(pool, deps, job, owner, turn, draft or "")


async def _run_stt(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    audio_path: str,
    duration_seconds: float,
    deadline: float,
) -> tuple[str | None, tuple[ErrorCode, str] | None]:
    config: AppConfig = deps.config
    settings = config.settings
    price_min = settings.costs.stt_per_minute_usd or 0.0
    from decimal import Decimal

    upper_bound = Decimal(str(price_min)) * Decimal(str(max(duration_seconds, 1.0) / 60.0))
    provider = "unknown"
    model = "unknown"
    if config.provider_profile is not None:
        provider = config.provider_profile.stt.provider or "unknown"
        model = config.provider_profile.stt.model or "unknown"

    call_index = 0
    pause_min = settings.limits.provider_retry_pause_min_seconds
    pause_max = settings.limits.provider_retry_pause_max_seconds
    while call_index < settings.limits.max_provider_calls_per_operation:
        if time.monotonic() >= deadline:
            return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_deadline")
        async with pool.acquire() as conn:
            existing = await jobs_repo.count_provider_calls(conn, job["id"])
            if existing >= settings.limits.max_provider_calls_per_operation:
                break
            call_index = existing + 1
            call_id = await token_budget.reserve_call(
                conn,
                job_id=job["id"],
                attempt=call_index,
                operation="stt",
                provider=provider,
                model=model,
                profile_version="2.0",
                input_tokens=0,
                output_tokens=0,
                reserved_usd=upper_bound.quantize(Decimal("0.000001")),
            )
        stt_started = time.monotonic()
        try:
            text = await deps.stt.transcribe(audio_path, language="ru")
            async with pool.acquire() as conn:
                await token_budget.settle_call(
                    conn, call_id, outcome="success", actual_usd=upper_bound
                )
            metrics.record_call("stt", metrics.OUTCOME_SUCCESS, stt_started)
            return text, None
        except SttRetryableError as exc:
            metrics.record_call("stt", metrics.outcome_for_error(exc), stt_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="known_failure")
            if time.monotonic() + max(pause_min, 1) > deadline:
                return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_retry_deadline")
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="stt")
            await asyncio.sleep(max(0.0, random.uniform(pause_min, pause_max)))
        except (SttConfigError, SttFatalError) as exc:
            metrics.record_call("stt", metrics.outcome_for_error(exc), stt_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="known_failure")
            logger.error("stt config/fatal failure")
            return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_fatal")
        except SttUnknownResultError as exc:
            metrics.record_call("stt", metrics.outcome_for_error(exc), stt_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="unknown")
            if time.monotonic() >= deadline:
                return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_unknown_deadline")
            metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="stt")
        except (TimeoutError, OSError) as exc:
            metrics.record_call("stt", metrics.outcome_for_error(exc), stt_started)
            async with pool.acquire() as conn:
                await token_budget.settle_call(conn, call_id, outcome="unknown")
            if time.monotonic() >= deadline:
                return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_call_deadline")
    return None, (ErrorCode.TRANSCRIPTION_FAILED, "stt_calls_exhausted")


async def _save_draft(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    owner: str,
    turn: asyncpg.Record,
    draft: str,
) -> None:
    """Черновик + экран подтверждения (§3.5, §3.7). Каждая новая расшифровка
    увеличивает transcript_version; прежние кнопки недействительны."""
    config: AppConfig = deps.config
    from app.storage import users_repo

    async with pool.acquire() as conn, conn.transaction():
        # Канонический порядок блокировок (§2.3): users -> sessions -> jobs
        session_pre = await sessions_repo.find_by_id(conn, job["session_id"])
        if session_pre is None:
            return
        user = await users_repo.lock_by_id(conn, session_pre["user_id"])
        if user is None:
            return
        session = await sessions_repo.find_by_id(conn, job["session_id"], for_update=True)
        job_now = await jobs_repo.lock_for_commit(conn, job["id"])
        if job_now is None or job_now["state"] != "leased" or job_now["lease_owner"] != owner:
            return
        if (
            session is None
            or session["status"] != SessionStatus.transcribing.value
            or session["current_turn_id"] != turn["id"]
            or session["version"] != job["expected_version"]
        ):
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner, result={"stale": True}
            )
            return
        version = await conn.fetchval(
            """
            UPDATE turns
            SET draft_transcript = $2,
                transcript_version = COALESCE(transcript_version, 0) + 1
            WHERE id = $1
            RETURNING transcript_version
            """,
            turn["id"],
            draft,
        )
        await sessions_repo.transition(
            conn,
            session["id"],
            SessionStatus.confirming_transcript,
            expected_statuses=(SessionStatus.transcribing,),
        )
        await render_transcript_confirmation(
            conn,
            config,
            user=user,
            session_id=session["id"],
            turn_id=turn["id"],
            draft=draft,
            version=version,
        )
        await jobs_repo.mark_done(
            conn, job["id"], owner=owner,
            result={"transcript_version": version, "length": len(draft)},
        )


async def render_transcript_confirmation(
    conn: asyncpg.Connection,
    config: AppConfig,
    *,
    user: asyncpg.Record,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    draft: str,
    version: int,
) -> None:
    """Экран подтверждения расшифровки: фактический текст и четыре действия
    (§3.7). Смена черновика делает прежние кнопки недействительными."""
    await ui_tokens_repo.revoke_unconsumed(
        conn,
        user["id"],
        actions=("confirm_transcript", "edit_transcript", "rerecord_voice", "skip_turn"),
    )
    ttl = timedelta(hours=config.settings.ui_tokens.question_control_hours)
    buttons = []
    for action, label, allowed in (
        ("confirm_transcript", messages.TRANSCRIPT_CONFIRM_LABEL, f"{turn_id}:{version}"),
        ("edit_transcript", messages.TRANSCRIPT_EDIT_LABEL, f"{turn_id}:{version}"),
        ("rerecord_voice", messages.TRANSCRIPT_RERECORD_LABEL, str(turn_id)),
        ("skip_turn", messages.SKIP_LABEL, str(turn_id)),
    ):
        token = generate_ui_token()
        await ui_tokens_repo.issue(
            conn,
            token=token,
            user_id=user["id"],
            action=action,
            ttl=ttl,
            allowed_value=allowed,
            session_id=session_id,
        )
        buttons.append([(label, f"{action}:{token}")])
    await outbox_repo.enqueue(
        conn,
        unique_key=f"transcript:{turn_id}:{version}",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="text",
        payload=text_payload(
            user["chat_id"], messages.TRANSCRIPT_CONFIRM.format(draft=draft)
        ),
        session_id=session_id,
        is_required=True,
    )
    await outbox_repo.enqueue(
        conn,
        unique_key=f"transcript-buttons:{turn_id}:{version}",
        user_id=user["id"],
        chat_id=user["chat_id"],
        kind="buttons",
        payload=buttons_payload(user["chat_id"], "Выберите действие:", buttons),
        session_id=session_id,
        is_required=True,
    )


async def _fail_to_awaiting(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    owner: str,
    turn: asyncpg.Record,
    session: asyncpg.Record,
    failure: tuple[ErrorCode, str],
    *,
    message_override: str | None = None,
) -> None:
    """Ошибка распознавания: awaiting_answer, черновик не засчитан, аудио
    удалено (finally вызывающего кода); сообщение с причиной (§3.2)."""
    metrics.REGISTRY.inc(
        metrics.FALLBACK, operation="stt", reason=failure[1] or failure[0].value
    )
    code, detail = failure
    from app.domain.errors import ERROR_REGISTRY
    from app.storage import users_repo

    text = message_override or ERROR_REGISTRY[code][0]
    async with pool.acquire() as conn, conn.transaction():
        # users -> sessions -> jobs (§2.3)
        user = await users_repo.lock_by_id(conn, session["user_id"])
        session_now = await sessions_repo.find_by_id(conn, session["id"], for_update=True)
        job_now = await jobs_repo.lock_for_commit(conn, job["id"])
        if job_now is None or job_now["state"] != "leased" or job_now["lease_owner"] != owner:
            return
        if session_now is not None and session_now["status"] == SessionStatus.transcribing.value:
            await sessions_repo.transition(
                conn,
                session["id"],
                SessionStatus.awaiting_answer,
                expected_statuses=(SessionStatus.transcribing,),
            )
        await conn.execute(
            "UPDATE turns SET draft_transcript = NULL WHERE id = $1", turn["id"]
        )
        if user is not None:
            await session_service.enqueue_notice(
                conn,
                user=user,
                text=text,
                unique_key=f"stt-failed:{turn['id']}:{job['attempts']}",
                session_id=session["id"],
            )
        await jobs_repo.mark_failed(
            conn, job["id"], owner=owner, error_code=code.value,
            result={"detail": detail},
        )


async def fail_transcribe_job(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str, detail: str
) -> None:
    """Отказ распознавания без вызова провайдера (очередь/слот): сессия
    возвращается в awaiting_answer, черновик не засчитывается (§3.2)."""
    async with pool.acquire() as conn:
        turn = await turns_repo.find_by_id(conn, job["turn_id"])
        session = await sessions_repo.find_by_id(conn, job["session_id"])
    if turn is None or session is None:
        await _mark(job, owner, pool, "failed", error_code="CONTEXT_GONE")
        return
    await _fail_to_awaiting(
        pool, deps, job, owner, turn, session,
        (ErrorCode.TRANSCRIPTION_FAILED, detail),
    )


async def fail_tts_job(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str, detail: str
) -> None:
    """Сбой озвучивания не блокирует интервью (§3.5)."""
    async with pool.acquire() as conn:
        turn = await turns_repo.find_by_id(conn, job["turn_id"])
        session = await sessions_repo.find_by_id(conn, job["session_id"])
    if turn is None or session is None:
        await _mark(job, owner, pool, "failed", error_code="CONTEXT_GONE")
        return
    await _tts_unavailable(pool, deps, job, owner, session, turn, detail)


async def _set_media_key(pool: asyncpg.Pool, job: asyncpg.Record, value: str | None) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET media_storage_key = $2 WHERE id = $1", job["id"], value
            )
    except (asyncpg.PostgresError, OSError):  # pragma: no cover
        logger.warning("media key update failed")


async def _mark(
    job: asyncpg.Record,
    owner: str,
    pool: asyncpg.Pool,
    state: str,
    *,
    result: dict | None = None,
    error_code: str = "",
) -> None:
    async with pool.acquire() as conn:
        if state == "done":
            await jobs_repo.mark_done(conn, job["id"], owner=owner, result=result or {})
        else:
            await jobs_repo.mark_failed(
                conn, job["id"], owner=owner, error_code=error_code, result=result
            )


# ---------------------------------------------------------------------------
# TTS (§3.5): озвучивание доставленного текстового вопроса
# ---------------------------------------------------------------------------


async def process_tts_job(
    pool: asyncpg.Pool, deps: Any, job: asyncpg.Record, owner: str
) -> None:
    config: AppConfig = deps.config
    settings = config.settings
    audio_path: Path | None = None
    try:
        async with pool.acquire() as conn:
            turn = await turns_repo.find_by_id(conn, job["turn_id"])
            session = await sessions_repo.find_by_id(conn, job["session_id"])
        if turn is None or session is None or session["format"] != "voice":
            await _mark(job, owner, pool, "done", result={"skipped": True})
            return
        if turn["delivered_message_id"] is None:
            # текст вопроса ещё не подтверждён — озвучка не отправляется раньше
            await _mark(job, owner, pool, "done", result={"skipped": "text_not_delivered"})
            return
        question_text = turn["question_text"][: settings.limits.question_max_chars]

        if deps.tts is None or not config.ai_enabled:
            await _tts_unavailable(pool, deps, job, owner, session, turn, "tts_disabled")
            return

        try:
            check_capacity(config.env.media_dir, settings.limits.media_total_max_mib,
                           incoming_bytes=5 * 1024 * 1024)
        except MediaStorageFullError:
            await _tts_unavailable(pool, deps, job, owner, session, turn, "storage_full")
            return

        audio_path = new_media_path(config.env.media_dir)
        create_private_file(audio_path)
        await _set_media_key(pool, job, str(audio_path))

        from decimal import Decimal

        price_1k = settings.costs.tts_per_1k_chars_usd or 0.0
        upper_bound = (
            Decimal(str(price_1k)) * Decimal(str(len(question_text))) / Decimal(1000)
        ).quantize(Decimal("0.000001"))
        provider = "unknown"
        model = "unknown"
        if config.provider_profile is not None:
            provider = config.provider_profile.tts.provider or "unknown"
            model = config.provider_profile.tts.model or "unknown"

        deadline = time.monotonic() + settings.timeouts_seconds.tts_deadline_total
        duration: float | None = None
        call_index = 0
        pause_min = settings.limits.provider_retry_pause_min_seconds
        pause_max = settings.limits.provider_retry_pause_max_seconds
        while call_index < settings.limits.max_provider_calls_per_operation:
            if time.monotonic() >= deadline:
                break
            async with pool.acquire() as conn:
                existing = await jobs_repo.count_provider_calls(conn, job["id"])
                if existing >= settings.limits.max_provider_calls_per_operation:
                    break
                call_index = existing + 1
                call_id = await token_budget.reserve_call(
                    conn,
                    job_id=job["id"],
                    attempt=call_index,
                    operation="tts",
                    provider=provider,
                    model=model,
                    profile_version="2.0",
                    input_tokens=estimate_tokens(question_text),
                    output_tokens=0,
                    reserved_usd=upper_bound,
                )
            tts_started = time.monotonic()
            try:
                duration = await deps.tts.synthesize(question_text, str(audio_path))
                async with pool.acquire() as conn:
                    await token_budget.settle_call(
                        conn, call_id, outcome="success", actual_usd=upper_bound
                    )
                metrics.record_call("tts", metrics.OUTCOME_SUCCESS, tts_started)
                break
            except TtsRetryableError as exc:
                metrics.record_call("tts", metrics.outcome_for_error(exc), tts_started)
                async with pool.acquire() as conn:
                    await token_budget.settle_call(conn, call_id, outcome="known_failure")
                if time.monotonic() + max(pause_min, 1) > deadline:
                    duration = None
                    break
                metrics.REGISTRY.inc(metrics.PROVIDER_RETRIES, operation="tts")
                await asyncio.sleep(max(0.0, random.uniform(pause_min, pause_max)))
            except (TtsConfigError, TtsFatalError) as exc:
                metrics.record_call("tts", metrics.outcome_for_error(exc), tts_started)
                async with pool.acquire() as conn:
                    await token_budget.settle_call(conn, call_id, outcome="known_failure")
                duration = None
                break
            except (TimeoutError, TtsUnknownResultError, OSError) as exc:
                metrics.record_call("tts", metrics.outcome_for_error(exc), tts_started)
                async with pool.acquire() as conn:
                    await token_budget.settle_call(conn, call_id, outcome="unknown")
                duration = None
                if time.monotonic() >= deadline:
                    break

        if duration is None:
            await _tts_unavailable(pool, deps, job, owner, session, turn, "tts_failed")
            return

        # Озвучка — необязательная строка outbox: ответ на текстовый вопрос;
        # аудио дополнение не меняет текущий turn (§3.5)
        from app.storage import users_repo

        async with pool.acquire() as conn, conn.transaction():
            user = await users_repo.lock_by_id(conn, session["user_id"])
            session_now = await sessions_repo.find_by_id(conn, session["id"], for_update=True)
            job_now = await jobs_repo.lock_for_commit(conn, job["id"])
            if (
                job_now is None
                or job_now["state"] != "leased"
                or job_now["lease_owner"] != owner
            ):
                remove_file(audio_path)
                return
            if user is None or session_now is None:
                remove_file(audio_path)
                return
            await outbox_repo.enqueue(
                conn,
                unique_key=f"voice:{turn['id']}",
                user_id=user["id"],
                chat_id=user["chat_id"],
                kind="question_voice",
                payload={
                    "method": "sendVoice",
                    "chat_id": user["chat_id"],
                    "voice_path": str(audio_path),
                    "reply_to_message_id": turn["delivered_message_id"],
                },
                session_id=session["id"],
                is_required=False,
                reply_to_message_id=turn["delivered_message_id"],
            )
            await jobs_repo.mark_done(
                conn, job["id"], owner=owner,
                result={"duration_seconds": duration, "storage": str(audio_path)},
            )
        audio_path = None  # файл принадлежит outbox; удалит sender/sweep
    finally:
        if audio_path is not None:
            remove_file(audio_path)
            await _set_media_key(pool, job, None)


async def _tts_unavailable(
    pool: asyncpg.Pool,
    deps: Any,
    job: asyncpg.Record,
    owner: str,
    session: asyncpg.Record,
    turn: asyncpg.Record,
    detail: str,
) -> None:
    """Сбой озвучивания не блокирует ответ (§3.5)."""
    from app.storage import users_repo

    async with pool.acquire() as conn, conn.transaction():
        user = await users_repo.lock_by_id(conn, session["user_id"])
        job_now = await jobs_repo.lock_for_commit(conn, job["id"])
        if job_now is None or job_now["state"] != "leased" or job_now["lease_owner"] != owner:
            return
        if user is not None:
            await session_service.enqueue_notice(
                conn,
                user=user,
                text=messages.TTS_UNAVAILABLE,
                unique_key=f"tts-failed:{turn['id']}",
                session_id=session["id"],
            )
        await jobs_repo.mark_failed(
            conn, job["id"], owner=owner, error_code="TTS_UNAVAILABLE",
            result={"detail": detail},
        )
        metrics.REGISTRY.inc(metrics.FALLBACK, operation="tts", reason=detail)
