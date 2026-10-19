# Чеклист соответствия части 4 требованиям ТЗ 2.0

Формат: требование ТЗ (раздел) → где реализовано → чем проверено.
Задачи: T13, T14 (отчёт и доставка), T15, T16, T17 (голос: приём аудио,
STT-подтверждение, TTS). Всего 60 новых тестов (422 в проекте).

## T13 — Отчёт и доказательства по ответам (§2.2, §3.4)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Отчёт только из фактических вопросов/ответов/оценок сессии; снимок без Telegram ID и username (§3.4, §3.9) | `report_service.build_snapshot` (ReportSnapshot 2.0 из turns/assessments) | test_report_saved_before_sending_reproducible; unit-тесты детерминированного отчёта | готово |
| FeedbackReport 2.0: summary, strengths[], gaps[], evidence-backed recommendations (1–3 конкретных действия), next_steps (§3.4) | `_call_llm_report` → parse + Pydantic `FeedbackReport` (extra=forbid) | test_report_delivery_completes_session; contracts/schemas | готово |
| Цитаты буквально из ответов (NFC + пробелы), без выдуманного опыта (§3.4, AT10) | `validate_report_payload` → contracts/semantic_validation | test_fabricated_quote_degrades, test_invalid_saved_quote_ignored | готово |
| ≤2 вызова провайдера на отчёт; повтор при невалидном JSON (§3.2) | учёт ai_calls UNIQUE(job_id, attempt) | test_invalid_report_json_degrades, test_extra_text_around_json_degrades | готово |
| Резерв стоимости до вызова; успех/отказ/unknown — как в T11 (§3.10) | token_budget.reserve_call/settle_call (operation='report') | test_token_overflow_uses_deterministic_report (журнал затрат) | готово |
| Ответы не помещаются в лимит → детерминированный отчёт по сохранённым оценкам, помечен degraded; без фиктивных цитат (§3.4) | `build_deterministic_report` + `format_report_text` | test_token_overflow_uses_deterministic_report, test_no_assessments_no_fabrication | готово |
| Невалидный вывод модели после повтора → тот же детерминированный запасной сценарий, degraded (§3.2) | `_apply_report_fallback` | test_invalid_report_json_degrades | готово |
| Отчёт ≤12000 символов, 1–4 части ≤3500, цитаты не рвутся (§3.4) | `split_into_parts`, `shrink_to_limit` | test_report_format (13 unit-тестов): границы, цитаты, huge→12000 | готово |
| Частичное интервью помечено («Неполное интервью»), пропуски видны (§3.4, AT09) | `compute_partial` + секция пропусков | test_partial_label_on_early_finish, test_compute_partial, test_skipped_main_is_not_partial | готово |
| Отчёт сохраняется до отправки; все части — обязательные строки outbox одной транзакцией (§2.2) | `_save_and_enqueue` (reports + outbox + jobs атомарно) | test_report_saved_before_sending_reproducible | готово |

## T14 — Доставка отчёта и учёт попытки (§2.4, §3.4)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Последовательная доставка частей в порядке seq (§2.2) | outbox_worker: FIFO по (seq, created_at) в пределах сессии | test_report_delivery_completes_session | готово |
| delivery_started_at до первой попытки; после — отмена возвращает BUSY «отчёт уже отправляется» (§2.4) | `mark_delivery_started` в `_deliver`; `cancel_session_tx` → BUSY(CANCEL_ALREADY_SENDING) | test_cancel_after_delivery_started_is_busy | готово |
| До начала отправки отменить можно, резерв освобождается (§2.4) | cancel_session_tx (delivery_started_at IS NULL) | test_dialog_flow (отмена активной), part2-чеклист | готово |
| Все части доставлены → completed + резерв попытки → consumed (§2.4) | `on_report_part_delivered_tx` | test_report_delivery_completes_session | готово |
| Терминальный сбой доставки (400/403/401) → failed, резерв освобождается (§2.4, AT15) | `_fail_row` → `on_report_delivery_failed_tx` | test_report_delivery_failure_releases_credit | готово |
| Повторный /report возвращает сохранённый отчёт без нового LLM-вызова и без списания (§2.2, §3.4) | `_handle_report_command` → re-enqueue сохранённых частей | test_report_command_redelivers_without_new_attempt | готово |
| Оценка полезности (да/нет) — одно событие на сессию; «Новая тренировка» (§3.7) | кнопки report_rating_* / new_training, events dedup_key | test_report_rating_is_single_event, test_new_training_restarts_entry | готово |
| Событие report_delivered по бизнес-переходу, не по update_id (§3.10) | events_repo.emit(dedup_key='report_delivered:{session}') | test_report_rating_is_single_event (события) | готово |

## T15 — Приём аудио и ограничения (§3.5)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Только OGG/Opus voice; аудио-документ отклоняется с подсказкой (§3.5) | классификация update в dispatch + `media_service.probe` | test_audio_document_rejected_with_hint, test_bad_container_rejected | готово |
| ≤10 MiB: лимит по метаданным до скачивания + жёсткая обрезка потока (отказ, не частичный файл) (§3.5) | `bot.download_file(max_bytes)`; submit_voice_tx | test_metadata_duration_rejected_before_download, test_oversized_stream_rejected | готово |
| ≤120 с фактической длительности (granule-позиция последней страницы OGG) (§3.5) | `media_probe` (изолированный парсер) | test_probe_duration_over_limit_rejected, test_valid_opus_duration | готово |
| Декодер — отдельный процесс: RLIMIT_AS 256 MiB, RLIMIT_CPU/wall 10 с, без shell (§3.5) | `media_probe.run_probe` (subprocess + rlimit) | test_run_probe_subprocess_ok/rejects_bad, test_broken_page_sync | готово |
| Каталог 0700, файлы 0600, случайные 32-символьные имена (§3.5, §3.9) | `media_service.store` | test_dir_0700_file_0600_random_names | готово |
| 256 MiB и число файлов на сервер; при заполнении новый голосовой вход отклоняется без принятия ответа (§3.10) | `MediaStorageFullError` → ErrorCode.MEDIA_STORAGE_FULL | test_media_storage_full_refuses_entry, test_capacity_* | готово |
| Удаление: finally обработки, sweep каждые 5 минут, startup cleanup, максимум 1 час (§3.9) | `remove`, `sweep_expired`, `startup_cleanup`; цикл sweep в workers/main | test_sweep_expired_only_old, test_startup_cleanup_removes_all, test_remove_file_idempotent | готово |
| File URL с токеном бота не логируется и не уходит провайдеру (§3.9) | скачивание только внутри bot-адаптера | обзор кода; логи без URL | готово |

## T16 — STT и подтверждение расшифровки (§3.5)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Transcribe-задание; ≤2 STT-вызова на операцию; ≤3 запуска STT на вопрос (§3.2, §3.5, AT11) | `process_transcribe_job`; счётчик stt_attempts у turn | test_stt_retries_capped_at_two_calls, test_three_stt_attempts_per_question | готово |
| Черновик draft_transcript + transcript_version; подтверждение защищает от старой записи (§3.5) | `render_transcript_confirmation`; confirm с expected version | test_happy_path_confirm_transcript, test_stale_version_confirm_rejected | готово |
| Исправление текста (1–4000) и перезапись без расхода turn (§3.5) | edit_transcript → ui_state editing_transcript; rerecord_voice → awaiting_answer | test_edit_transcript_then_confirm; state-machine.json (rerecord) | готово |
| Пустая расшифровка и >4000 символов не засчитываются (§3.5) | проверки в accept/confirm | test_empty_transcript_not_counted, test_too_long_transcript_not_counted | готово |
| Сбой STT → возврат к ожиданию ответа; текст всегда доступен (§3.5) | `_stt_failure` → awaiting_answer + сообщение | test_stt_failure_returns_to_awaiting | готово |
| Голос во время расшифровки — BUSY «подождите» (§3.5) | проверка статуса confirming/transcribing | test_voice_busy_while_transcribing | готово |
| Пропуск с экрана подтверждения (§3.7) | skip_turn из confirming_transcript | test_skip_from_confirmation_screen | готово |
| Неподтверждённый черновик не попадает в отчёт; удаляется при отказе/замене, максимум 1 час (§3.9) | отчёт использует только confirmed answer_text; sweep | test_voice_flow (snapshot), sweep-тесты | готово |

## T17 — TTS и возврат к тексту (§3.5)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Озвучивается только текст вопроса ≤700; без ответа пользователя (§3.5, §3.9) | `process_tts_job` (question_text) | test_tts_voice_follows_delivered_text | готово |
| Голос — reply на доставленный текстовый вопрос; необязательная строка outbox (§3.5) | kind='question_voice', reply_to delivered_message_id | test_tts_voice_follows_delivered_text | готово |
| Сбой TTS не блокирует вопрос: текст уже доставлен (§3.5, AT03) | `_tts_failure` → notice, сессия не меняется | test_tts_failure_does_not_block | готово |
| ≤2 TTS-вызова на операцию; бюджет как у LLM (§3.2, §3.10) | ai_calls operation='tts' + token_budget | test_tts_retries_capped_at_two_calls | готово |
| Результат TTS удаляется после доставки (§3.9) | `_cleanup_voice` в outbox после success | test_media_service (remove), обзор outbox_worker | готово |

## Сквозная механика части 4

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Маршрутизация заданий assess/report/transcribe/tts; семафоры 5/2/2 (§1.3, AT14) | jobs_worker: per-kind Semaphore | конфигурация settings.limits.concurrent_* | готово (глобальный контроль — T19) |
| Поздний результат отменённой/удалённой сессии не применяется (§2.4) | expected_version + lease_owner guard-ы в report/transcribe/tts | test_cancelled_session_result_not_applied (part3), guard-ы voice/report | готово |
| Классификация ошибок STT/TTS: 429/5xx/сеть — повтор; 400 — фатально; 401/403 — сигнал (§3.2) | providers/stt.py, tts.py (SttRetryable/Fatal/ConfigError и TTS-аналоги) | unit-тесты провайдеров (part1-стиль стаба) | готово |
| Тексты состояний и ошибок голоса/отчёта (§3.7) | app/telegram/messages.py | интеграционные тесты потока | готово |

Примечание: AT03/AT11 (полное голосовое интервью на живых провайдерах)
и AT10 (18 предметных эталонов с рецензентом) требуют внешних сервисов
и предметной проверки — задачи T21–T23 части 5.
