# Чеклист соответствия части 5 требованиям ТЗ 2.0

Формат: требование ТЗ (раздел) → где реализовано → чем проверено.
Задачи: T18, T19, T20, T21 (механика), T22, T23 (артефакты), T24 (материалы).

## T18 — Отмена, истечение и удаление (§3.9, §3.10)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Неактивная сессия истекает через 24 ч; фоновые повторы срок не продлевают | expires_at обновляется только touch_user_action; `lifecycle.expire_sessions` (sweep каждые 5 мин) | test_expire_inactive_session_releases_reserve | готово |
| Истечение освобождает резерв, отменяет jobs и outbox | expire_sessions → release_by_session/cancel_by_session/cancel_pending | test_expire_cancels_jobs_and_pending_outbox | готово |
| Контент отменённых/истёкших удаляется в течение 24 ч; завершённых — 30 дней; очистка каждый час | `purge_session_content` (retention sweep 60 мин) | test_cancelled_content_purged_after_24_hours, test_completed_content_purged_after_30_days | готово |
| Минимальная терминальная запись sessions сохраняется (параметры, статус, счётчики, время) для связи consumed-credit | sessions не удаляются при очистке контента | test_completed_content_purged (session row + consumed credit) | готово |
| Перед удалением turns обнулить current_turn_id | purge_session_content (UPDATE до DELETE) | test_completed_content_purged (current_turn_id NULL) | готово |
| /report после очистки не восстанавливает содержание | отчёты удалены; диспетчер определяет срок по терминальной записи | test_report_command_after_purge_does_not_restore (REPORT_EXPIRED) | готово |
| Неподтверждённый черновик STT — максимум 1 час; не используется в отчёте | `purge_stt_drafts`; отчёт только из подтверждённых answer_text | test_stt_draft_purged_after_one_hour | готово |
| inbox/outbox payload обнуляется ≤24 ч после обработки; незавершённые — не дольше дедлайна 24 ч | `purge_queue_payloads` (processed→NULL+mark; stale pending→skipped/cancelled) | test_queue_payloads_nullified_after_24h, test_stale_pending_outbox_cancelled_at_deadline | готово |
| Метаданные завершённых очередей — 7 дней | `purge_queue_metadata` (jobs/inbox/outbox/ui_tokens) | test_queue_metadata_removed_after_7_days | готово |
| Неизвестный результат учитывается до сверки: unknown-журнал не удаляется 7-дневной очисткой (предел 90 дней) | purge_queue_metadata удерживает jobs с unknown ai_calls | test_unknown_cost_journal_held_until_reconciliation | готово |
| События и журнал AI-затрат — 90 дней | `purge_events_and_costs` | test_events_and_costs_removed_after_90_days | готово |
| Tombstone — 7 дней | `purge_tombstones` | test_tombstones_expire_after_7_days | готово |
| /delete ≤24 ч: блокировка (ч.2) → отмена очередей → удаление данных и медиа → удаление пользователя | `process_user_deletions` (media после commit) | test_user_deletion_removes_data_media_and_creates_tombstone | готово |
| Tombstone содержит только прежний внутренний UUID; отдельный журнал удаления | deletion_tombstones (user_id без FK) | тот же тест (содержимое tombstone) | готово |
| После restore удаления повторяются по tombstones ДО приёма трафика | `reapply_tombstones`; startup-сверка worker; scripts/reapply_tombstones.py; restore.sh шаг 3 | test_reapply_tombstones_after_restore | готово |
| Восстановленный inbox сверяется с профилями и tombstones до обработки | `purge_inbox_of_deleted_users`; `_is_tombstoned` в InboxWorker; deletion_requested_at guard в dispatch | test_restored_inbox_of_tombstoned_user_skipped, test_worker_skips_claimed_update_of_tombstoned_user | готово |
| Поздние результаты не восстанавливают данные (AT06) | expected_version/lease_owner/terminal guards (ч.3–4); jobs отменяются при expire/delete | test_at06_late_ai_result_after_cancel/expiry_discarded | готово |
| Старые кнопки не управляют терминальной сессией; удалённые сообщения не появляются | _bound_session STALE guard; ui_tokens CASCADE; outbox skip при удалении | test_at06_old_buttons…, test_deleted_user_old_buttons_do_not_recreate_data | готово |
| Технические логи 14 дней — ротация без текстов ответов | logging max-size/max-file в compose; формат логов без контента (ч.1) | docker-compose.yml; deploy-checklist §1.10 | готово (артефакт) |

## T19 — Лимиты, бюджет и метрики (§3.10)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Учёт всех попыток API: вызовы провайдеров по операциям и исходам (success/429/5xx/400/401-403/unknown/invalid_json) | `metrics.record_call`/`outcome_for_error` на границе порта (assess/report/voice сервисы) | test_pipeline_increments_core_counters, test_invalid_json_outcome_counted, unit test_metrics | готово |
| Число API-повторов | PROVIDER_RETRIES (assess/report/stt/tts) | test_invalid_json_outcome_counted (retries ≥1) | готово |
| Доля невалидного JSON | PROVIDER_CALLS{outcome=invalid_json} | test_invalid_json_outcome_counted | готово |
| fallback/degraded | FALLBACK{operation,reason}; REPORT_DEGRADED | test_fallback_and_degraded_counters | готово |
| Стоимость (резерв/факт/unknown) | COST_*_USD_MICROS в token_budget; daily_usage — источник истины | test_pipeline (cost counters); test_at08_unknown_result | готово |
| Успех report-delivery | REPORT_DELIVERED{outcome} (success в on_report_part_delivered_tx; failed в outbox) | test_fallback_and_degraded_counters; test_acceptance AT15 | готово |
| STT-подтверждения/перезаписи/исправления | STT_ACTION{action=confirmed/edited/rerecorded} | test_stt_action_counters | готово |
| Обработка inbox, lease-expiry, задержки этапов | INBOX_PROCESSED, INBOX/JOB_LEASE_EXPIRED, LATENCY_INBOX/JOB/OUTBOX/PROVIDER (p50/p95) | test_pipeline_increments_core_counters; лог `metrics` раз в минуту | готово |
| Размер и возраст очередей | `monitoring.collect_gauges` (inbox/jobs/outbox pending+oldest) | test_collect_gauges_* | готово |
| Метрики без содержимого ответов | только имена/метки-категории/числа | test_snapshot_has_no_content_fields | готово |
| Бизнес-события: start, first_answer, answer_accepted, report_delivered, report_rating, trial_consumed; дедупликация по переходу | events_repo.emit (dedup_key) — все шесть (ч.2–4) | test_report_rating_is_single_event; CHECK-словарь events | готово |
| Сигнал: oldest AI job > 60 с | evaluate_signals (gauges.ai_oldest_job_seconds) | test_evaluate_signals_fires_all_categories | готово |
| Сигнал: degraded > 10% за последние 20 отчётов | evaluate_signals (окно из settings.alerts) | test_evaluate_signals_* | готово |
| Сигнал: heartbeat worker отсутствует > 60 с | файл RUN_DIR/worker_heartbeat.json (запись каждые 20 с); монитор в worker + check_signals | test_heartbeat_file_roundtrip, test_evaluate_signals_quiet/fires | готово |
| Сигнал: ошибка backup | RUN_DIR/backup_status.json пишет scripts/backup.sh; оценка возраста/ok | test_backup_error_signal_on_failed_status | готово |
| Сигнал: 401/403 AI | ERROR-лог адаптера + приращение config_error-счётчика в мониторе worker-процесса | test_evaluate_signals (ai_auth_error_401_403, без повтора) | готово |
| Сигнал: остаток суточного бюджета < 20% | evaluate_signals (daily_usage vs daily_ai_budget_usd) | test_daily_budget_low_signal | готово |
| Канал уведомлений задаётся перед запуском; сигналы — не автоотправка людям | emit_signals → ERROR-лог `operator_signal`; deploy-checklist §0 | test_emit_signals_logs_without_content | готово (канал — условие запуска) |
| Лимиты 3 старта/24 ч, 10 команд/мин, 1 сессия (повторная проверка T19) | ч.2 (daily_usage/sessions guards) | test_dialog_flow; test_at15_one_active_session | готово |
| Суточный лимит блокирует дорогие задания, сохраняя права и запасной результат | token_budget.daily_budget_remaining (ч.3) | test_daily_budget_blocks_expensive_call (ч.3) | готово |

## T20 — Интеграция, гонки и сбои (§3.11)

| Сценарий | Реализация | Проверка | Статус |
|---|---|---|---|
| AT05: 10 повторов update и 2 одновременных ответа — без повторного job/списания; исходный ответ не меняется | дедупликация inbox PK; BUSY-guard принятого turn; блокировки строки turn | test_at05_ten_duplicate_updates_single_effect, test_at05_two_concurrent_answers_one_wins (настоящий gather) | готово |
| AT06: старые кнопки cancel/finish/delete и поздний AI — терминал не восстанавливается; удалённое не появляется | STALE guards; expected_version; tombstones; outbox skip | test_at06_* (4 теста), test_lifecycle (delete/restore) | готово |
| AT07: падение до/после commit и неизвестный sendMessage — ответ не теряется; повтор допустим и обозначен; reply по известным IDs; порядок отчёта сохранён | сохранение до подтверждения; _safe_release; receipts/attempts; seq-порядок | test_at07_crash_before_commit…, test_at07_commit_before_send…, test_at07_unknown_send_result… | готово |
| AT08: таймауты/429/5xx/невалидный JSON — ≤2 вызова; нет зависших lease; запасной сценарий и резерв соблюдены | ai_calls UNIQUE(job,attempt); recover_expired_leases; fallback-ветки | test_at08_429_and_5xx_capped…, test_at08_invalid_json_twice…, test_at08_unknown_result_keeps_reserve | готово |
| AT15: одна активная сессия; резерв/consumed атомарны; отказ освобождает право; неизвестный charge в расходах | start_session_tx; on_report_part_delivered_tx; on_report_delivery_failed_tx; daily_usage.unknown_usd | test_at15_* (3 теста) | готово |
| Обнаружен и устранён дефект: /finish из awaiting_answer ронял report-задание на неотвеченном turn (бесконечный краш-цикл) | finish_session_tx помечает текущий turn пропущенным; build_snapshot трактует turn без ответа как skipped | test_finish_from_awaiting_marks_current_skipped | готово |

## T21 — Качество AI и эталоны (§3.11 AT04/AT10)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| 18 эталонных наборов: 3 роли × 2 уровня × 3 качества; технический и HR-ответ, ожидаемые рубрики, допустимое действие, недопустимые выдуманные утверждения | tests/references/reference_sets.json (reviewer_status=pending) | verify_spec.py (check_references) | наборы подготовлены разработчиком |
| Допустимые переходы слабый/средний/сильный по фиксированным правилам §3.3 | compute_allowed_action/compute_next_difficulty | test_reference_transitions_follow_fixed_rules (18), test_reference_dont_know…, test_reference_follow_up_limits (18) | готово (автоматическая часть) |
| Уточнение использует сложность родителя; шаг сложности — после основного технического ответа (включая ветку уточнения) | _apply_assessment: шаг до follow-up-ветки; _create_follow_up(parent.difficulty) | test_reference_set_end_to_end (18 сквозных прогонов) | готово; уточнение трактовки §3.3 зафиксировано тестами |
| Конкретный разбор без выдумок: цитаты проверяемы, действия 1–3, degraded ≤1 на 18 эталонах | validate_report; FakeLlm-прогон (degraded=0 во всех 18) | test_reference_set_end_to_end (asserts по отчёту), test_fabricated_experience_rejected_by_validator (18) | готово (автоматическая часть) |
| Предметная проверка эталонов рецензентом (2–4 ч) — не подменяется JSON-проверкой | reviewer_status в наборах; handoff-protocol (рецензент) | — | условие запуска |
| Прогон AT04/AT10 на живом провайдере | deploy-checklist §1.7; load-test-protocol §3 | — | условие запуска (T23) |

## T22 — Нагрузка, интерфейс и задержки (§1.3, AT14)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| 20 активных сессий; 5 LLM / 2 STT / 2 TTS; остальные в очереди | jobs_worker: конкурентная обработка (in-flight = сумма слотов), семафоры по видам | test_llm_concurrency_capped_at_five (пик ≤5 и ≥2), test_stt_and_tts_concurrency_capped_at_two | готово |
| Очередь AI ≤60 с, затем запасной сценарий без вызова | queue_budget в _process | test_queue_overload_falls_back_after_budget | готово |
| p95 шести этапов по определениям §1.3 (received_at → delivered_at) | scripts/load_test.py (замеры по меткам БД) | planning/load-test-protocol.md + load-2026-10-18.json: все p95 в целях, 20/20 completed | готово (ёмкость конвейера) |
| Протокол на реальных провайдерах и Telegram-клиентах (Android/iOS/Desktop, AT16) | load-test-protocol.md §3 (шаги и критерий приёмки) | — | условие запуска |

## T23 — Развёртывание, эксплуатация и backup (§3.10, AT17)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Контейнеры API/worker/PostgreSQL + reverse proxy TLS; dev/staging отдельные боты | docker-compose.yml (loopback-порт, internal-сеть, ENV_NAME) | deploy-checklist §0/§3 | готово (артефакты) |
| Роли БД: приложение/миграции/backup | deploy/init-db-roles.sql; MIGRATIONS_DATABASE_URL | deploy-checklist §0 | готово (артефакт) |
| При запуске: переменные/профиль/settings → миграции → worker → webhook (секрет, allowed_updates) → health → первый вопрос → оба формата и отчёт | startup-валидация (ч.1); migrate; set_webhook.py; smoke.py; deploy-checklist §1 | smoke.py (локальная проверка: SMOKE OK при живом API) | готово (последовательность) |
| Фиксировать версии зависимостей, контейнера, миграции, профиля и промптов | requirements pinned; Dockerfile base; make_version_manifest.py → version-manifest.json | verify_spec.py (prompts hashes) | готово |
| Backup ежедневно в защищённое хранилище; шифрование; скользящие 7 дней; статус для сигнала | scripts/backup.sh (pg_dump ролью backup + openssl AES-256 + rolling + backup_status) | bash -n; мониторинг backup_error | готово (артефакт; фактический cron — условие запуска) |
| Restore: трафик только после повторного удаления; без дублирования прав и отправок | scripts/restore.sh (stop → restore → reapply_tombstones → start → smoke) | test_reapply_tombstones_after_restore; restore.sh порядок шагов | готово (артефакт) |
| RPO ≤24 ч, RTO ≤2 ч — проектные цели, подтверждаются восстановлением staging | deploy-checklist §2 (журнал RPO/RTO) | — | условие запуска (drill на staging) |
| /health/ready не зависит от кратковременного отказа LLM; конфигурационная ошибка → неготов | health.py (только БД+конфиг) | test_health.py (ч.1) | готово |
| Временное аудио ≤256 MiB на сервер; при заполнении отказ без принятия ответа | media_service (ч.4) | test_media_storage_full_refuses_entry (ч.4) | готово |

## T24 — Передача (§5.1–§5.3, AT18)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Повестка 45 мин (5/10/10/10/10); четыре сложные ситуации; шаблон протокола | planning/handoff-protocol.md | обзор | материалы готовы |
| Доступы проверяются smoke, а не устным обещанием (репозиторий+CI, бот+webhook, миграция/backup/restore, LLM JSON, русский STT, OGG/Opus TTS, health, уведомление) | planning/access-register.csv (A1–A8, S1); scripts (smoke/set_webhook/backup/restore) | — | материалы готовы; заполнение при передаче |
| Секреты через защищённое хранилище | access-register S1; .env.example без значений | verify_spec.py (check_secrets) | готово |
| План и оценки: wbs.csv, estimates.json (90/18/30/6, критический путь 58 ч) | planning/wbs.csv; estimates.json | verify_spec.py (check_wbs) | готово |
| Матрица соответствия (67+ детализаций) | planning/checklist-matrix.csv (69 строк) | verify_spec.py (check_matrix) | готово |
| Воспроизводимая проверка пакета: verification/verify_spec.py + spec-check.json | 10 проверок (schemas/examples/catalog/settings/states/wbs/matrix/prompts/secrets/references); шаг CI | verify_spec.py: overall ok (10/10) | готово |
| Диаграммы компонентов, потоков, сущностей | diagrams/components.svg, dataflow.svg, entities.svg | обзор | готово |
| README: порядок чтения и запуск проверки | README.md (§«Порядок чтения пакета») | обзор | готово |
| Фактическая встреча, подтверждение доступов, принятая версия/провайдеры/бюджет | handoff-protocol (шаблон фиксации) | — | условие запуска (§5.3: пункт выполнен только после встречи) |

Примечание: коммерческий контур P01–P06 (Telegram Stars) в часть 5 не входит —
отдельная итерация после функционального MVP; billing_enabled=false до проверок
§3.8/§3.11 (схема payments создана, charge_id защищает от двойного начисления).
