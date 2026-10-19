"""Фейки и harness для интеграционных тестов части 2.

Harness прогоняет полный конвейер: webhook-сохранение обновления в inbox ->
inbox-worker (фаза 1 + транзакция) -> outbox-worker (FakeBot). Позволяет
проверять состояния БД и фактически «отправленные» сообщения без Telegram.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

import asyncpg

from app.config import load_config
from app.storage import inbox_repo
from app.telegram.bot import SendOutcome, SendResult
from app.telegram.updates import classify_update
from app.workers.dispatch import WorkerDeps
from app.workers.inbox_worker import InboxWorker
from app.workers.outbox_worker import OutboxWorker, RateLimiter


class FakeBot:
    """Заменяет aiogram-клиент; записывает отправки, умеет скрипт исходов."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.voices: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.download_script: deque[Any] = deque()
        self.voice_script: deque[SendResult | None] = deque()
        self.acks: list[str] = []
        self.edits: list[tuple[int, int, str]] = []
        self.script: deque[SendResult | None] = deque()
        self._rule: tuple[Any, SendResult, int | None] | None = None
        self._next_message_id = 1000

    def queue_result(self, result: SendResult | None) -> None:
        self.script.append(result)

    def queue_outcomes(self, *outcomes: SendOutcome) -> None:
        for outcome in outcomes:
            if outcome is SendOutcome.success:
                self.script.append(None)
            else:
                self.script.append(SendResult(outcome, error="scripted"))

    def set_failure_rule(
        self,
        predicate: Any,
        result: SendResult,
        times: int | None = None,
    ) -> None:
        """Отказ для сообщений, удовлетворяющих предикату (по тексту payload).
        times=None — без ограничения числа срабатываний."""
        self._rule = (predicate, result, times)

    def clear_rules(self) -> None:
        self.script.clear()
        self._rule = None

    async def send_message(self, payload: dict[str, Any]) -> SendResult:
        self.sent.append(payload)
        if self._rule is not None:
            predicate, result, times = self._rule
            if predicate(payload) and (times is None or times > 0):
                if times is not None:
                    self._rule = (predicate, result, times - 1)
                return result
        if self.script:
            scripted = self.script.popleft()
            if scripted is not None:
                return scripted
        self._next_message_id += 1
        return SendResult(SendOutcome.success, message_id=self._next_message_id)

    async def send_voice(self, payload: dict[str, Any]) -> SendResult:
        self.voices.append(payload)
        if self.voice_script:
            scripted = self.voice_script.popleft()
            if scripted is not None:
                return scripted
        self._next_message_id += 1
        return SendResult(SendOutcome.success, message_id=self._next_message_id)

    async def download_file(
        self, file_id: str, dest_path: str, max_bytes: int
    ):
        from app.telegram.bot import DownloadOutcome, DownloadResult

        content = self.files.get(file_id)
        if content is None:
            if self.download_script:
                scripted = self.download_script.popleft()
                if scripted is not None:
                    return scripted
            return DownloadResult(DownloadOutcome.not_found, error="no_file")
        if self.download_script:
            scripted = self.download_script.popleft()
            if scripted is not None:
                return scripted
        if len(content) > max_bytes:
            # поток обрезается на пределе: частичная запись + too_large
            with open(dest_path, "wb") as fh:
                fh.write(content[: max_bytes + 1])
            return DownloadResult(DownloadOutcome.too_large, size_bytes=max_bytes + 1)
        with open(dest_path, "wb") as fh:
            fh.write(content)
        return DownloadResult(DownloadOutcome.success, size_bytes=len(content))

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        self.acks.append(callback_query_id)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> SendResult:
        self.edits.append((chat_id, message_id, text))
        return SendResult(SendOutcome.success, message_id=message_id)

    async def close(self) -> None:
        pass

    # --- helpers to inspect sent messages ---

    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.sent]

    def last_text(self) -> str:
        return self.sent[-1].get("text", "") if self.sent else ""

    def buttons_of(self, index: int) -> list[tuple[str, str]]:
        markup = self.sent[index].get("reply_markup") or {}
        rows = markup.get("inline_keyboard") or []
        return [(b["text"], b["callback_data"]) for row in rows for b in row]

    def find_button(self, action: str, message_index: int = -1) -> str | None:
        """callback_data первой найденной кнопки с данным действием."""
        indexes = (
            range(len(self.sent) - 1, -1, -1)
            if message_index == -1
            else [message_index]
        )
        for i in indexes:
            for _text, data in self.buttons_of(i):
                if data.startswith(f"{action}:"):
                    return data
        return None

    def has_force_reply(self, index: int = -1) -> bool:
        markup = self.sent[index].get("reply_markup") or {}
        return bool(markup.get("force_reply"))


class FakeLlm:
    """LLM-клиент для тестов: по умолчанию строит валидный TurnAssessment 2.0
    из фактических данных входа (цитата — буквальная подстрока ответа)."""

    def __init__(self, responder: Any = None) -> None:
        from app.providers.llm import LlmResult  # noqa: F401

        self.responder = responder  # (data: dict, call_no: int) -> str | Exception
        self.delay = 0.0
        self.calls: list[Any] = []
        self.parsed_inputs: list[dict[str, Any]] = []

    @staticmethod
    def default_assessment(data: dict[str, Any], *, topic_score: int = 3,
                           relevance: int = 3, missing: list[str] | None = None,
                           action: str = "next_topic",
                           next_question: str | None = None) -> dict[str, Any]:
        answer = data["answer"]
        quote = answer.strip().split(".")[0][:80] or answer[:40]
        scores: dict[str, Any] = {
            "relevance": relevance,
            "structure": 3,
            "correctness": None,
            "evidence": None,
        }
        if data["question_type"] == "technical":
            scores["correctness"] = topic_score
        else:
            scores["evidence"] = topic_score
        return {
            "turn_id": data["turn_id"],
            "scores": scores,
            "missing_points": missing or [],
            "evidence_quotes": [quote] if quote.strip() else [],
            "explanation": "Ответ по существу вопроса, структура ясная.",
            "suggested_action": action,
            "next_question": next_question,
        }

    @staticmethod
    def default_report(data: dict[str, Any]) -> dict[str, Any]:
        """Валидный FeedbackReport 2.0 из фактического снимка: цитаты —
        буквальные подстроки ответов, действия связаны с пробелами."""
        turns = data["snapshot_turns"]
        mains = [t for t in turns if t["kind"] == "main"]
        strengths: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for t in turns:
            assessment = t.get("assessment")
            if t.get("skipped"):
                gaps.append({
                    "turn_id": t["turn_id"], "quote": None, "skipped": True,
                    "comment": f"Тема «{t['topic']}» пропущена.",
                })
                continue
            if not assessment:
                continue
            answer = t.get("answer_text") or ""
            quotes = [q for q in assessment.get("evidence_quotes", []) if q and q in answer]
            score = (assessment["scores"].get("correctness")
                     or assessment["scores"].get("evidence") or 0)
            if score >= 3 and quotes and len(strengths) < 5:
                strengths.append({
                    "turn_id": t["turn_id"], "quote": quotes[0], "skipped": False,
                    "comment": f"Тема «{t['topic']}»: ответ раскрыт на {score} из 4.",
                })
            if assessment.get("missing_points") and quotes and len(gaps) < 5:
                gaps.append({
                    "turn_id": t["turn_id"], "quote": quotes[0], "skipped": False,
                    "comment": "Не раскрыто: " + ", ".join(assessment["missing_points"][:3]) + ".",
                })
        actions: list[dict[str, Any]] = []
        for g in gaps[:3]:
            actions.append({
                "priority": len(actions) + 1, "related_turn_id": g["turn_id"],
                "text": "Повторите тему и потренируйте ответ вслух.",
            })
        if not actions:
            anchor = next((t for t in reversed(turns) if not t.get("skipped")), turns[0])
            actions.append({
                "priority": 1, "related_turn_id": anchor["turn_id"],
                "text": "Закрепите результат новой тренировкой через несколько дней.",
            })
        return {
            "session_id": data["session_id"],
            "partial": data["partial"],
            "coverage": {
                "main_total": len(mains),
                "main_answered": sum(1 for t in mains if not t.get("skipped")),
                "main_skipped": sum(1 for t in mains if t.get("skipped")),
                "follow_ups": sum(1 for t in turns if t["kind"] == "follow_up"),
            },
            "strengths": strengths,
            "gaps": gaps,
            "actions": actions,
            "improved_examples": [],
        }

    async def complete(self, request: Any):
        import asyncio as _asyncio

        from app.providers.llm import LlmResult

        self.calls.append(request)
        data = json.loads(request.user_payload)
        self.parsed_inputs.append(data)
        call_no = len(self.calls)
        if self.delay:
            await _asyncio.sleep(self.delay)
        if self.responder is not None:
            out = self.responder(data, call_no)
            if isinstance(out, Exception):
                raise out
            content = out
        elif getattr(request, "operation", "assess") == "report":
            content = json.dumps(self.default_report(data), ensure_ascii=False)
        else:
            content = json.dumps(self.default_assessment(data), ensure_ascii=False)
        return LlmResult(
            content=content,
            input_tokens=max(1, len(request.system_prompt + request.user_payload) // 2),
            output_tokens=max(1, len(content) // 2),
        )

    async def close(self) -> None:
        pass


def make_ogg_opus(duration_seconds: float, *, rate: int = 48000) -> bytes:
    """Синтетический OGG/Opus для тестов: BOS-страница с OpusHead и
    EOS-страница с granule position = rate * длительность."""
    import struct

    def page(header_type: int, granule: int, seq: int, payload: bytes) -> bytes:
        header = b"OggS" + bytes([0, header_type]) + struct.pack("<q", granule)
        header += struct.pack("<I", 0x5A5A5A5A) + struct.pack("<I", seq)
        header += struct.pack("<I", 0)  # CRC не проверяется структурным парсером
        header += bytes([1, len(payload)])
        return header + payload

    head = b"OpusHead" + b"\x01\x02" + struct.pack("<I", rate) + b"\x00" * 7
    return page(0x02, 0, 0, head) + page(
        0x04, int(rate * duration_seconds), 1, b"\xfc" * 20
    )


class FakeStt:
    """STT-клиент для тестов: скрипт результатов (текст или исключение)."""

    def __init__(self) -> None:
        self.scripts: deque[Any] = deque()
        self.calls: list[str] = []
        self.default_text = "Учусь на третьем курсе, делаю учебный проект на Python."

    async def transcribe(self, file_path: str, *, language: str = "ru") -> str:
        self.calls.append(file_path)
        if self.scripts:
            item = self.scripts.popleft()
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return item
        return self.default_text

    async def close(self) -> None:
        pass


class FakeTts:
    """TTS-клиент для тестов: пишет синтетический OGG/Opus."""

    def __init__(self) -> None:
        self.scripts: deque[Any] = deque()
        self.calls: list[str] = []

    async def synthesize(self, text: str, dest_path: str) -> float:
        self.calls.append(text)
        if self.scripts:
            item = self.scripts.popleft()
            if isinstance(item, Exception):
                raise item
        with open(dest_path, "wb") as fh:
            fh.write(make_ogg_opus(3.0))
        return 3.0

    async def close(self) -> None:
        pass


class Harness:
    def __init__(
        self,
        pool: asyncpg.Pool,
        env: dict[str, str],
        bot: FakeBot | None = None,
        llm: FakeLlm | None = None,
        stt: FakeStt | None = None,
        tts: FakeTts | None = None,
    ):
        self.config = load_config(env)
        self.pool = pool
        self.bot = bot or FakeBot()
        self.llm = llm or FakeLlm()
        self.stt = stt or FakeStt()
        self.tts = tts or FakeTts()
        limiter = RateLimiter(global_per_second=5000, per_chat_per_second=5000)
        self.deps = WorkerDeps(
            config=self.config, bot=self.bot, limiter=limiter, llm=self.llm,
            stt=self.stt, tts=self.tts,
        )
        self.inbox = InboxWorker(pool, self.deps)
        self.outbox = OutboxWorker(pool, self.deps)
        self.outbox.limiter = limiter
        from app.workers.jobs_worker import JobsWorker

        self.jobs = JobsWorker(pool, self.deps)
        self._update_id = 100_000
        self._message_id = 10

    # --- входящие обновления ---

    def next_update_id(self) -> int:
        self._update_id += 1
        return self._update_id

    async def push(self, update: dict[str, Any]) -> None:
        """Эмулирует webhook + полную обработку worker-ом."""
        kind, supported = classify_update(update)
        async with self.pool.acquire() as conn:
            await inbox_repo.insert_update(
                conn,
                update_id=update["update_id"],
                kind=kind,
                payload=update,
                initial_status="pending" if supported else "skipped",
            )
        await self.process_inbox()
        await self.drain_outbox()

    async def send_text(
        self,
        text: str,
        *,
        chat_id: int = 555,
        reply_to_message_id: int | None = None,
        update_id: int | None = None,
    ) -> int:
        self._message_id += 1
        message: dict[str, Any] = {
            "message_id": self._message_id,
            "date": 1760000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
        }
        if reply_to_message_id is not None:
            message["reply_to_message"] = {"message_id": reply_to_message_id}
        uid = update_id or self.next_update_id()
        await self.push({"update_id": uid, "message": message})
        return self._message_id

    async def send_voice(
        self,
        *,
        chat_id: int = 555,
        duration: int = 10,
        reply_to: int | None = None,
        file_id: str = "AgADBA",
    ) -> None:
        content = self.bot.files.setdefault(file_id, make_ogg_opus(max(duration, 1)))
        self._message_id += 1
        message: dict[str, Any] = {
            "message_id": self._message_id,
            "date": 1760000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "voice": {
                "file_id": file_id,
                "duration": duration,
                "file_size": len(content),
            },
        }
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        await self.push({"update_id": self.next_update_id(), "message": message})

    async def click(
        self, callback_data: str, *, chat_id: int = 555, update_id: int | None = None
    ) -> None:
        update = {
            "update_id": update_id or self.next_update_id(),
            "callback_query": {
                "id": f"cb-{callback_data[:12]}",
                "from": {"id": chat_id},
                "chat_instance": "-1",
                "data": callback_data,
            },
        }
        await self.push(update)

    # --- обработка ---

    async def process_inbox(self, max_items: int = 50) -> int:
        processed = 0
        for _ in range(max_items):
            item = await self.inbox._claim_one()
            if item is None:
                break
            await self.inbox._process(item)
            processed += 1
        return processed

    async def drain_outbox(self, max_items: int = 100) -> int:
        delivered = 0
        for _ in range(max_items):
            row = await self.outbox._claim()
            if row is None:
                break
            await self.outbox._deliver(row)
            delivered += 1
        return delivered

    async def run_jobs(self, max_items: int = 10) -> int:
        processed = 0
        for _ in range(max_items):
            job = await self.jobs._claim()
            if job is None:
                break
            await self.jobs._process(job)
            processed += 1
        return processed

    async def run_cycle(self, max_rounds: int = 8) -> None:
        """inbox -> jobs -> outbox до полного затухания."""
        for _ in range(max_rounds):
            n = await self.process_inbox()
            n += await self.run_jobs()
            n += await self.drain_outbox()
            if n == 0:
                break

    # --- состояния БД ---

    async def fetch_user(self, telegram_id: int = 555) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_user_id = $1", telegram_id
            )

    async def fetch_active_session(self, telegram_id: int = 555) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.* FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE u.telegram_user_id = $1
                  AND s.status NOT IN ('completed','cancelled','expired','failed')
                """,
                telegram_id,
            )

    async def fetch_turns(self, session_id) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM turns WHERE session_id = $1 ORDER BY ordinal", session_id
            )

    async def fetch_credits(self, telegram_id: int = 555) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT c.* FROM credits c JOIN users u ON u.id = c.user_id
                WHERE u.telegram_user_id = $1 ORDER BY created_at
                """,
                telegram_id,
            )

    async def fetch_jobs(self, unique_key: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM jobs WHERE unique_key = $1", unique_key)

    async def fetch_session(self, session_id) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)

    async def fetch_report(self, session_id) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM reports WHERE session_id = $1", session_id
            )

    async def fetch_jobs_by_kind(self, kind: str) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jobs WHERE kind = $1 ORDER BY created_at", kind
            )
        return list(rows)

    async def fetch_outbox(self, unique_key: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM outbox WHERE unique_key = $1", unique_key
            )

    async def fetch_inbox(self, update_id: int) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await inbox_repo.get_update(conn, update_id)

    # --- сценарные сокращения ---

    async def button_for(self, action: str, allowed_value: str | None = None) -> str:
        """callback_data кнопки действия с нужным разрешённым значением.

        Токены непрозрачны (§3.1): соответствие значения проверяется по БД —
        так же, как это делает сервер при клике.
        """
        candidates: list[str] = []
        for payload in self.bot.sent:
            markup = payload.get("reply_markup") or {}
            for row in markup.get("inline_keyboard") or []:
                for button in row:
                    data = button.get("callback_data") or ""
                    if data.startswith(f"{action}:"):
                        candidates.append(data)
        for data in reversed(candidates):
            token = data.split(":", 1)[1]
            async with self.pool.acquire() as conn:
                record = await conn.fetchrow(
                    "SELECT action, allowed_value, consumed_at, expires_at "
                    "FROM ui_tokens WHERE token = $1",
                    token,
                )
            if record is None or record["action"] != action:
                continue
            if record["consumed_at"] is not None:
                continue
            if allowed_value is None or record["allowed_value"] == allowed_value:
                return data
        raise AssertionError(
            f"кнопка {action}={allowed_value} не найдена; отправлено: {self.bot.texts()[-3:]}"
        )

    async def complete_entry(self, chat_id: int = 555, *, role: str = "python",
                             level: str = "junior", fmt: str = "text") -> None:
        """/start -> (согласие) -> роль -> уровень -> формат -> Начать."""
        await self.send_text("/start", chat_id=chat_id)
        user = await self.fetch_user(chat_id)
        if (user is not None and user["consent_version"] is None) or user is None:
            await self.click(await self.button_for("consent_accept"), chat_id=chat_id)
        await self.click(await self.button_for("select_role", role), chat_id=chat_id)
        await self.click(
            await self.button_for("select_level", f"{role}:{level}"), chat_id=chat_id
        )
        await self.click(
            await self.button_for("select_format", f"{role}:{level}:{fmt}"), chat_id=chat_id
        )
        await self.click(
            await self.button_for("start_session", f"{role}:{level}:{fmt}"), chat_id=chat_id
        )


    async def cancel_active(self, chat_id: int = 555) -> None:
        """Отмена активной сессии через кнопки (с подтверждением)."""
        await self.click(await self.button_for("cancel_session"), chat_id=chat_id)
        await self.click(await self.button_for("cancel_confirm"), chat_id=chat_id)


async def make_harness(db_url: str, env: dict[str, str]) -> tuple[Harness, asyncpg.Pool]:
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    return Harness(pool, env), pool
