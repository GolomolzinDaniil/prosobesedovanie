"""Голосовой режим (§3.5; задачи T15, T16, T17).

Приём voice с ограничениями до скачивания, изолированная проверка
формата/длительности, STT с черновиком и подтверждением (≤3 запуска на
вопрос), исправление и перезапись, TTS-озвучка вопроса после текста,
отказы, не блокирующие интервью.
"""

from __future__ import annotations

import os

import pytest

from app.domain.errors import ERROR_REGISTRY
from app.providers.stt import SttFatalError, SttRetryableError
from app.providers.tts import TtsFatalError
from app.telegram import messages
from contracts.models import ANSWER_MAX_CHARS, ErrorCode
from tests.fakes import make_ogg_opus

pytestmark = pytest.mark.asyncio


async def voice_session(harness):
    """Voice-сессия с доставленным первым вопросом."""
    await harness.complete_entry(fmt="voice")
    session = await harness.fetch_active_session()
    turn = (await harness.fetch_turns(session["id"]))[0]
    return session, turn


async def transcribe_once(harness, turn, *, file_id="AgADBA", duration=10):
    """Отправка голоса + обработка transcribe-задания + доставка экрана."""
    await harness.send_voice(reply_to=turn["delivered_message_id"],
                             duration=duration, file_id=file_id)
    active = await harness.fetch_active_session()
    assert active["status"] == "transcribing"
    await harness.run_jobs()
    await harness.drain_outbox()


# ---------------------------------------------------------------------------
# Приём и ограничения (§3.5, T15)
# ---------------------------------------------------------------------------


async def test_metadata_duration_rejected_before_download(harness) -> None:
    """Длительность из метаданных проверяется ДО скачивания (§1.3)."""
    session, turn = await voice_session(harness)
    await harness.send_voice(reply_to=turn["delivered_message_id"], duration=121)
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"  # распознавание не запускалось
    turn = (await harness.fetch_turns(session["id"]))[0]
    assert turn["stt_attempts"] == 0
    assert await harness.fetch_jobs_by_kind("transcribe") == []
    assert any(
        ERROR_REGISTRY[ErrorCode.VOICE_TOO_LONG][0] in t for t in harness.bot.texts()
    )


async def test_audio_document_rejected_with_hint(harness) -> None:
    """Документы и другие аудиоформаты отклоняются (§3.5)."""
    session, turn = await voice_session(harness)
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 9001,
            "date": 1760000000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555},
            "document": {"file_id": "doc1", "file_name": "song.mp3"},
        },
    }
    await harness.push(update)
    assert any(messages.VOICE_FORMAT_TEXT in t for t in harness.bot.texts())
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"


async def test_oversized_stream_rejected(harness) -> None:
    """Поток ограничен жёстким пределом 10 MiB до полного скачивания."""
    session, turn = await voice_session(harness)
    big = b"\x00" * (10 * 1024 * 1024 + 1)
    harness.bot.files["BIGFILE"] = big
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 9002,
            "date": 1760000000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555},
            "voice": {"file_id": "BIGFILE", "duration": 30},
        },
    }
    await harness.push(update)
    await harness.run_jobs()
    await harness.drain_outbox()
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    assert any(
        ERROR_REGISTRY[ErrorCode.VOICE_TOO_LARGE][0] in t for t in harness.bot.texts()
    )


async def test_bad_container_rejected(harness) -> None:
    """Фактический формат проверяется декодером: не OGG/Opus — отказ."""
    session, turn = await voice_session(harness)
    harness.bot.files["WAVFILE"] = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 9003,
            "date": 1760000000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555},
            "voice": {"file_id": "WAVFILE", "duration": 5},
        },
    }
    await harness.push(update)
    await harness.run_jobs()
    await harness.drain_outbox()
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    assert any(
        ERROR_REGISTRY[ErrorCode.VOICE_FORMAT_UNSUPPORTED][0] in t
        for t in harness.bot.texts()
    )


async def test_probe_duration_over_limit_rejected(harness) -> None:
    """Метаданные занижены: фактическая длительность 130 с > 120 с (§3.5)."""
    session, turn = await voice_session(harness)
    harness.bot.files["LONGFILE"] = make_ogg_opus(130.0)
    update = {
        "update_id": harness.next_update_id(),
        "message": {
            "message_id": 9004,
            "date": 1760000000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555},
            "voice": {"file_id": "LONGFILE", "duration": 10},
        },
    }
    await harness.push(update)
    await harness.run_jobs()
    await harness.drain_outbox()
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    assert any(
        ERROR_REGISTRY[ErrorCode.VOICE_TOO_LONG][0] in t for t in harness.bot.texts()
    )


async def test_media_storage_full_refuses_entry(harness) -> None:
    """При заполнении хранилища новый голосовой вход отклоняется без
    принятия ответа (§3.5). Используется разреженный файл-заполнитель."""
    session, turn = await voice_session(harness)
    media = harness.config.env.media_dir
    os.makedirs(media, exist_ok=True)
    filler = media / ("f" * 32 + ".ogg")
    limit = harness.config.settings.limits.media_total_max_mib
    with open(filler, "wb") as fh:
        fh.truncate((limit + 1) * 1024 * 1024)  # sparse: размер есть, диск не тратится
    try:
        await harness.send_voice(reply_to=turn["delivered_message_id"])
        active = await harness.fetch_active_session()
        assert active["status"] == "awaiting_answer"
        turn = (await harness.fetch_turns(session["id"]))[0]
        assert turn["stt_attempts"] == 0
        assert any(
            ERROR_REGISTRY[ErrorCode.MEDIA_STORAGE_FULL][0] in t
            for t in harness.bot.texts()
        )
    finally:
        filler.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Черновик и подтверждение (§3.5, §3.7, T16)
# ---------------------------------------------------------------------------


async def test_happy_path_confirm_transcript(harness) -> None:
    session, turn = await voice_session(harness)
    media = harness.config.env.media_dir
    os.makedirs(media, exist_ok=True)
    files_before = set(os.listdir(media))
    await transcribe_once(harness, turn)

    active = await harness.fetch_active_session()
    assert active["status"] == "confirming_transcript"
    turns = await harness.fetch_turns(session["id"])
    current = next(t for t in turns if t["id"] == turn["id"])
    assert current["transcript_version"] == 1
    assert current["draft_transcript"] == harness.stt.default_text
    assert current["answer_text"] is None  # ещё не засчитан

    # экран подтверждения: фактический текст и четыре действия
    assert any("Проверьте распознанный ответ" in t for t in harness.bot.texts())
    assert any(harness.stt.default_text in t for t in harness.bot.texts())
    for action in ("confirm_transcript", "edit_transcript", "rerecord_voice", "skip_turn"):
        await harness.button_for(action)

    # аудио удалено после обработки (§3.5): новых файлов не осталось
    assert set(os.listdir(media)) == files_before

    # подтверждение -> тот же поток, что и текст (§3.5)
    await harness.click(
        await harness.button_for("confirm_transcript", f"{turn['id']}:1")
    )
    active = await harness.fetch_active_session()
    assert active["status"] == "evaluating"
    await harness.run_jobs()
    await harness.drain_outbox()
    turns = await harness.fetch_turns(session["id"])
    answered = next(t for t in turns if t["id"] == turn["id"])
    assert answered["answer_text"] == harness.stt.default_text
    assert answered["draft_transcript"] is None
    assert answered["assessment"] is not None
    assert len(turns) == 2  # создан следующий вопрос


async def test_edit_transcript_then_confirm(harness) -> None:
    session, turn = await voice_session(harness)
    await transcribe_once(harness, turn)

    await harness.click(await harness.button_for("edit_transcript", f"{turn['id']}:1"))
    assert any(messages.TRANSCRIPT_EDIT_PROMPT in t for t in harness.bot.texts())

    draft_row = await harness.fetch_outbox(f"transcript:{turn['id']}:1")
    corrected = "Исправленный ответ: третий курс, проект по анализу данных."

    # текст БЕЗ reply на сообщение черновика — не принимается (§3.7)
    await harness.send_text(corrected)
    assert any(messages.REPLY_REQUIRED_HINT in t for t in harness.bot.texts())
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["transcript_version"] == 1

    # reply на черновик — новая версия, прежние кнопки недействительны
    await harness.send_text(corrected, reply_to_message_id=draft_row["message_id"])
    turns = await harness.fetch_turns(session["id"])
    current = next(t for t in turns if t["id"] == turn["id"])
    assert current["transcript_version"] == 2
    assert current["draft_transcript"] == corrected
    assert current["answer_text"] is None  # не засчитан до подтверждения

    # подтверждение новой версии засчитывает исправленный текст
    await harness.click(
        await harness.button_for("confirm_transcript", f"{turn['id']}:2")
    )
    active = await harness.fetch_active_session()
    assert active["status"] == "evaluating"
    await harness.run_jobs()
    await harness.drain_outbox()
    turns = await harness.fetch_turns(session["id"])
    answered = next(t for t in turns if t["id"] == turn["id"])
    assert answered["answer_text"] == corrected


async def test_stale_version_confirm_rejected(harness) -> None:
    """Подтверждение старой версии отклоняется (§3.5)."""
    session, turn = await voice_session(harness)
    await transcribe_once(harness, turn)
    old_confirm = await harness.button_for("confirm_transcript", f"{turn['id']}:1")

    # перезапись -> новая расшифровка (версия 2)
    await harness.click(await harness.button_for("rerecord_voice", str(turn["id"])))
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    assert any(messages.VOICE_RERECORD_PROMPT in t for t in harness.bot.texts())

    harness.bot.files["AgADBB"] = make_ogg_opus(8)
    harness.stt.default_text = "Вторая запись: уточнил формулировку ответа."
    await transcribe_once(harness, turn, file_id="AgADBB")
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["transcript_version"] == 2

    await harness.click(old_confirm)
    assert any(messages.STALE_BUTTON in t for t in harness.bot.texts())
    active = await harness.fetch_active_session()
    assert active["status"] == "confirming_transcript"  # версия 2 ждёт решения

    await harness.click(
        await harness.button_for("confirm_transcript", f"{turn['id']}:2")
    )
    active = await harness.fetch_active_session()
    assert active["status"] == "evaluating"


async def test_stt_failure_returns_to_awaiting(harness) -> None:
    session, turn = await voice_session(harness)
    harness.stt.scripts.append(SttFatalError(status=400))
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    await harness.run_jobs()
    await harness.drain_outbox()
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    assert any(
        messages.TRANSCRIPTION_FAILED_TEXT in t or "Не удалось распознать" in t
        for t in harness.bot.texts()
    )
    turns = await harness.fetch_turns(session["id"])
    current = next(t for t in turns if t["id"] == turn["id"])
    assert current["draft_transcript"] is None
    assert current["stt_attempts"] == 1  # запуск учтён, но вопрос не уменьшен


async def test_stt_retries_capped_at_two_calls(harness) -> None:
    """Предел два вызова на операцию, включая повтор (§3.2)."""
    session, turn = await voice_session(harness)
    harness.stt.scripts.append(SttRetryableError(status=500))
    harness.stt.scripts.append(SttRetryableError(status=500))
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    await harness.run_jobs()
    await harness.drain_outbox()
    assert len(harness.stt.calls) == 2
    jobs = await harness.fetch_jobs_by_kind("transcribe")
    async with harness.pool.acquire() as conn:
        calls = await conn.fetchval(
            "SELECT count(*) FROM ai_calls WHERE job_id = $1", jobs[0]["id"]
        )
    assert calls == 2
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"


async def test_empty_transcript_not_counted(harness) -> None:
    session, turn = await voice_session(harness)
    harness.stt.scripts.append("   ")
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    await harness.run_jobs()
    await harness.drain_outbox()
    assert any(messages.TRANSCRIPT_EMPTY in t for t in harness.bot.texts())
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["draft_transcript"] is None


async def test_too_long_transcript_not_counted(harness) -> None:
    session, turn = await voice_session(harness)
    harness.stt.scripts.append("с" * (ANSWER_MAX_CHARS + 1))
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    await harness.run_jobs()
    await harness.drain_outbox()
    assert any(messages.TRANSCRIPT_TOO_LONG in t for t in harness.bot.texts())
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"


async def test_three_stt_attempts_per_question(harness) -> None:
    """≤3 запуска распознавания на вопрос; лимит не сбрасывается (§3.5)."""
    session, turn = await voice_session(harness)
    for attempt in range(3):
        harness.stt.scripts.append(SttFatalError(status=400))
        file_id = f"F{attempt}"
        harness.bot.files[file_id] = make_ogg_opus(5)
        await harness.send_voice(
            reply_to=turn["delivered_message_id"], file_id=file_id
        )
        await harness.run_jobs()
        await harness.drain_outbox()
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["stt_attempts"] == 3
    assert len(await harness.fetch_jobs_by_kind("transcribe")) == 3

    # четвёртая запись отклоняется с предложением текста/пропуска
    harness.bot.files["F4"] = make_ogg_opus(5)
    await harness.send_voice(reply_to=turn["delivered_message_id"], file_id="F4")
    assert any(messages.STT_ATTEMPTS_EXHAUSTED in t for t in harness.bot.texts())
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["stt_attempts"] == 3
    assert len(await harness.fetch_jobs_by_kind("transcribe")) == 3
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"

    # текстовый ответ по-прежнему принимается (§3.5)
    await harness.send_text(
        "Отвечу текстом: третий курс, учебный проект.",
        reply_to_message_id=turn["delivered_message_id"],
    )
    active = await harness.fetch_active_session()
    assert active["status"] == "evaluating"


async def test_skip_from_confirmation_screen(harness) -> None:
    session, turn = await voice_session(harness)
    await transcribe_once(harness, turn)
    await harness.click(await harness.button_for("skip_turn", str(turn["id"])))
    await harness.run_jobs()
    await harness.drain_outbox()
    turns = await harness.fetch_turns(session["id"])
    current = next(t for t in turns if t["id"] == turn["id"])
    assert current["skipped"] is True
    assert current["answer_text"] is None  # черновик не стал ответом
    active = await harness.fetch_active_session()
    assert active["status"] in ("delivering_question", "awaiting_answer")


async def test_voice_busy_while_transcribing(harness) -> None:
    session, turn = await voice_session(harness)
    await harness.send_voice(reply_to=turn["delivered_message_id"])
    # вторая запись до результата первой не принимается (сессия в transcribing)
    harness.bot.files["X2"] = make_ogg_opus(5)
    await harness.send_voice(reply_to=turn["delivered_message_id"], file_id="X2")
    assert any(messages.TRANSCRIBING in t for t in harness.bot.texts())
    turns = await harness.fetch_turns(session["id"])
    assert next(t for t in turns if t["id"] == turn["id"])["stt_attempts"] == 1
    assert len(await harness.fetch_jobs_by_kind("transcribe")) == 1


# ---------------------------------------------------------------------------
# TTS-озвучка вопроса (§3.5, T17)
# ---------------------------------------------------------------------------


async def test_tts_voice_follows_delivered_text(harness) -> None:
    session, turn = await voice_session(harness)
    # после доставки текста вопроса создано tts-задание
    tts_jobs = await harness.fetch_jobs_by_kind("tts")
    assert len(tts_jobs) == 1
    await harness.run_jobs()
    voice_rows = [
        r for r in (await harness.fetch_outbox(f"voice:{turn['id']}"),) if r
    ]
    assert voice_rows and voice_rows[0]["kind"] == "question_voice"
    assert voice_rows[0]["is_required"] is False
    await harness.drain_outbox()

    # озвучка отправлена ответом на текстовый вопрос (§3.5)
    assert len(harness.bot.voices) == 1
    assert harness.bot.voices[0]["reply_to_message_id"] == turn["delivered_message_id"]
    # озвучен только текст вопроса без ответа пользователя
    assert harness.tts.calls == [turn["question_text"][:700]]
    # результат TTS удалён после доставки
    assert not os.path.exists(harness.bot.voices[0]["voice_path"])
    # состояние сессии не изменилось: ждём ответ
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"


async def test_tts_failure_does_not_block(harness) -> None:
    """Сбой озвучивания: уведомление и продолжение интервью (§3.5)."""
    session, turn = await voice_session(harness)
    harness.tts.scripts.append(TtsFatalError(status=400))
    await harness.run_jobs()
    await harness.drain_outbox()
    assert any(messages.TTS_UNAVAILABLE in t for t in harness.bot.texts())
    active = await harness.fetch_active_session()
    assert active["status"] == "awaiting_answer"
    # интервью продолжается обычным текстовым ответом
    await harness.send_text(
        "Текстовый ответ вместо голоса.", reply_to_message_id=turn["delivered_message_id"]
    )
    active = await harness.fetch_active_session()
    assert active["status"] == "evaluating"


async def test_tts_retries_capped_at_two_calls(harness) -> None:
    from app.providers.tts import TtsRetryableError

    session, turn = await voice_session(harness)
    harness.tts.scripts.append(TtsRetryableError(status=500))
    harness.tts.scripts.append(TtsRetryableError(status=500))
    await harness.run_jobs()
    await harness.drain_outbox()
    assert len(harness.tts.calls) == 2
    assert any(messages.TTS_UNAVAILABLE in t for t in harness.bot.texts())
