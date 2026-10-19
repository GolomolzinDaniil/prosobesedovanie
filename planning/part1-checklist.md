# Чеклист соответствия части 1 требованиям ТЗ 2.0

Формат: требование ТЗ (раздел) → где реализовано → чем проверено.
Статус «готово» означает работающий код и прошедшие тесты части 1.

## T02 — Окружение, репозиторий, контейнеры, CI

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Структура репозитория (§3.10): app/api, telegram, domain, services, providers, storage, workers; migrations; config; prompts; tests | Корневая структура репозитория | README, обзор кода | готово |
| Один образ — два процесса (§2.1) | `Dockerfile` + команды `app.api.main` / `app.workers.main` / `app.storage.migrate` | smoke-запуск процесса | готово |
| Контейнеры: API, worker, PostgreSQL (§3.10) | `docker-compose.yml` (db, api, migrate, worker-profile) | YAML-валидация | готово (запуск в среде с Docker) |
| Воспроизводимый локальный запуск (T02) | README «Локальный запуск»; фиксированные версии `requirements.txt` | pytest, smoke | готово |
| CI: репозиторий и проверки (T02, §1.4) | `.github/workflows/ci.yml`: ruff, pytest с сервисным PostgreSQL, воспроизводимость examples/schemas | локальный прогон тех же команд | готово |
| Dev/staging отдельные боты и ключи (§3.10) | `ENV_NAME` (dev/staging/production) в EnvConfig; `.env.example` | test_env_config | готово |

## T03 — Модель данных и миграции

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| 14 таблиц (§2.3) | `migrations/0001_initial.sql` | test_all_14_tables_created | готово |
| UUID сервером, timestamptz UTC, nullable-правила (§2.3) | DEFAULT gen_random_uuid(), TIMESTAMPTZ, CHECK-пары | test_migrations | готово |
| Одна активная сессия на пользователя (§1.3) | частичный UNIQUE-индекс sessions_one_active_per_user | test_one_active_session_per_user | готово |
| finished_at в терминальном статусе (§2.3) | CHECK sessions_finished_required | test_terminal_session_requires_finished_at | готово |
| UNIQUE(session_id, ordinal), UNIQUE(session_id, main_index) для kind=main (§2.3) | ограничения turns | test_turns_constraints | готово |
| Максимум одно уточнение на parent_turn_id (§2.3) | частичный UNIQUE turns_one_follow_up_per_parent | test_turns_constraints | готово |
| Составной FK (turn_id, session_id); current_turn_id та же проверка (§2.3) | UNIQUE(id, session_id) + DEFERRABLE FK в sessions/jobs/turns | test_cross_session_turn_fk_rejected, test_current_turn_composite_fk | готово |
| Ответ 1–4000, вопрос 1–700 кодовых точек (§1.3) | CHECK char_length в turns | test_turns_constraints | готово |
| credits: UNIQUE trial на пользователя; payment_id+unit_index; reserved/consumed-пары (§2.3, §3.8) | ограничения credits | test_credit_* , test_payment_credit_requires_payment_fields | готово |
| payments: UNIQUE invoice_payload и charge_id; units по продукту; пустая в базовой схеме (§2.3, §3.8) | ограничения payments | test_payments_charge_id_unique_and_units_match | готово |
| inbox: update_id PK, статус/счётчики, payload удаляется, user_id nullable (§2.3) | схема inbox | test_webhook, test_inbox_claim | готово |
| jobs: unique_key UNIQUE, turn_id для assess/transcribe, lease (§2.3) | схема jobs | test_jobs_turn_required_for_assess | готово |
| outbox: unique_key, порядок, обязательность, receipts (§2.3) | UNIQUE(chat_id, seq), is_required, receipts JSONB | test_outbox_order_and_receipts | готово |
| ui_tokens: char(22), действие/значение/версия/срок (§3.1) | схема ui_tokens + CHECK действия | test_ui_tokens_and_tombstones | готово |
| events: dedup_key UNIQUE, разрешённые имена, ссылки обнуляются (§2.3, §3.10) | схема events, ON DELETE SET NULL | test_events_dedup_and_names | готово |
| daily_usage: неотрицательные decimal (§2.3) | CHECK | схема | готово |
| ai_calls: UNIQUE(job_id, attempt), фактическая стоимость NULL при неизвестном результате, содержимое запроса не хранится (§2.3, §3.10) | схема ai_calls | test_ai_calls_unique_and_cost_rules | готово |
| deletion_tombstones: без Telegram ID, срок (§2.3) | схема | test_ui_tokens_and_tombstones | готово |
| Миграции создаются и проверяются задачей T03; неизменяемость | раннер с SHA-256 и блокировкой правки | test_migrations_are_idempotent, test_tampered_migration_blocked | готово |

## T04 — Каталог вопросов

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| 6 сценариев роль × уровень, 36 основных записей (§3.3) | `config/questions.json` 2.0 | test_catalog_version_and_shape | готово |
| Сначала самопрезентация и проект, затем 4 технические темы по ролям (§3.3) | hr_topics + technical_topics, порядок main_index | test_topics_are_prescribed_per_role | готово |
| Рубрики, запасные вопросы, советы (§3.3) | rubric (key_points, evidence_criteria для HR, score_guidance 0–4), fallback_question, advice | test_every_entry_has_rubric_fallback_and_advice | готово |
| Сложности: intern 1–2, junior 1–3 (§3.3) | difficulty_variants по диапазону уровня | test_difficulty_variants_cover_level_range | готово |
| Длина вопроса ≤ 700 (§1.3) | валидация каталога + CHECK в turns | test_overlong_question_detected | готово |
| Версия фиксируется при старте сессии (§3.6) | sessions.catalog_version NOT NULL; загрузка версии 2.0 | схема; test_catalog | готово (фиксация при старте — часть 2) |
| Предметная проверка рецензентом (§3.3) | заготовки помечены в note каталога | — | ожидает рецензента (вне часов разработчика) |

## T05 — Промпты и эталоны

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Промпты — отдельные неизменяемые файлы версии 2.0 (§3.6) | `prompts/turn-system.txt`, `report-system.txt`, `user-envelope.txt` | test_prompts_are_versioned_files, test_missing_prompt_blocks_startup | готово |
| Ответ пользователя — данные в user message, не в системных инструкциях (§3.3) | user-envelope.txt, раздел «Правила безопасности» | обзор; подключение в части 3 | готово |
| TurnAssessment 2.0: поля, шкала 0–4, correctness/evidence по типу (§3.3) | `contracts/models.py` TurnScores/TurnAssessment | test_contracts_examples, test_semantic_validation | готово |
| FeedbackReport 2.0: coverage, ≤5/≤5, 1–3 действия, ≤3 примера, placeholders (§3.4) | FeedbackReport + ReportFinding/ReportAction/ImprovedExample | test_contracts_examples | готово |
| Цитаты буквально из ответа после NFC и унификации пробелов (§3.3–3.4) | `semantic_validation.normalize_for_quote/quote_matches` | test_normalize_nfc_and_whitespace, test_quote_* | готово |
| Запрет пропусков в strengths; связь действий с пробелами; приоритеты 1..n (§3.4) | `validate_report` | test_report_* | готово |
| Фиксированное правило действия и шага сложности (§3.3) | `compute_allowed_action`, `compute_next_difficulty` | test_action_*, test_difficulty_* | готово |
| Валидные/невалидные примеры с manifest (§5.1) | `contracts/examples/` (16 файлов) + manifest.json | test_contracts_examples (обход манифеста) | готово |
| Пример 75 XTR — проверка формата, не цена (§3.8) | successful-payment.json | test_successful_payment_example_is_75_xtr | готово |
| Схемы JSON (§5.1) | `contracts/schemas/` (35 файлов, экспорт из моделей) | CI-проверка воспроизводимости | готово |
| 18 эталонов AT04/AT10 с рецензентом (§3.11) | — | — | часть 3 (T21, требует рецензента) |

## T06 (API-часть) — Webhook, inbox, дедупликация

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Единственная точка POST /telegram/webhook/{route_token} по HTTPS (§3.1) | `app/api/routes/webhook.py`; TLS — reverse proxy (README) | test_webhook | готово |
| Секрет: неверный → 403; сравнение постоянное время (§3.1) | hmac.compare_digest | test_wrong_secret_403, test_missing_secret_header_403, test_secret_comparison_case_sensitive | готово |
| route_token не раскрывает точку (§3.1) | 404 при неверном токене | test_wrong_route_token_404 | готово |
| Только JSON ≤1 MiB: 415/413; неверная структура 400 (§3.1) | проверка content-type, Content-Length и потоковое чтение | test_non_json_415, test_oversized_413, test_invalid_structure_400 | готово |
| Поддерживаемый вид → inbox, 200 {"ok":true}; повтор update_id → 200 (§3.1) | insert ON CONFLICT DO NOTHING | test_valid_message_stored_pending, test_duplicate_update_id_acked_once | готово |
| Неподдерживаемый вид признан обработанным без AI (§3.1) | статус skipped | test_unsupported_kind_skipped_without_ai, test_pre_checkout_skipped_while_billing_disabled | готово |
| PostgreSQL недоступен → 503 (§3.1) | db=None или ошибка БД → 503 retryable | test_db_unavailable_503 | готово |
| allowed_updates: message, callback_query (+pre_checkout при оплате) (§3.1) | классификация видов; billing-флаг | test_update_classification | готово |
| /health/live, /health/ready без секретов и адреса БД (§3.1) | `routes/health.py` | test_health_* | готово |
| Единый ErrorResponse с request_id, включая отказы HTTP до Context (§3.2) | `errors_handlers.py` + middleware | test_error_body_shape_and_request_id, test_no_secrets_in_error_bodies | готово |
| Повтор update не повторяет команду; SKIP LOCKED + lease (§2.1, T06) | `inbox_repo.claim_batch/requeue/release_expired_leases` | test_claim_skips_locked_rows_between_consumers, test_concurrent_claims_do_not_overlap, test_expired_lease_returns_to_queue, test_requeue_backoff_and_max_retries (≤5) | готово |
| Транзакционный обработчик inbox (доменные эффекты) | механизм готов, обработчики — часть 2 | — | часть 2 |

## Сквозные требования, выполненные в части 1

| Требование | Реализация | Проверка |
|---|---|---|
| Нормализация ответа: NFC, CRLF→LF, strip, управляющие символы, длина после нормализации, без обрезания (§3.3) | `app/domain/answers.py` | test_domain_answers (0/1/4000/4001, NUL, табы/LF) |
| Ошибки: коды стабильны, message пользователю, retryable, retry_after (§3.2) | `app/domain/errors.py` + реестр 34 кодов | test_error_registry_covers_all_codes, test_rate_limited_requires_retry_after |
| UI-токен 22 ASCII (16 байт base64url), callback_data ≤64 (§1.4, §3.1) | `app/services/ui_tokens.py` | test_ui_tokens |
| Startup-валидация: max_turns, лимиты Telegram, резерв отчёта < бюджета, heartbeat < lease, целевые/аварийные времена, каталог, секреты (§3.6) | `app/config.py` | test_settings_validation (38 мутаций), test_env_config |
| Null в provider-profile = незаполненная зависимость, блокирует AI-вызовы (§1.4) | `ProviderProfile.is_complete` + ai_enabled | test_profile_with_nulls_blocks_ai, test_full_config_ai_enabled_with_filled_profile |
| products.json: single/pack5/pack10, units 1/5/10, XTR пустые, billing выключен (§3.8) | `config/products.json` + validate_products | test_settings_validation, schema |
| Секреты не в репозитории; логи без ответов и payload (§3.9) | `.gitignore`, `.env.example`, формат логов | test_no_secrets_in_error_bodies |
| Перечисления фиксированы конфигурацией, не свободным текстом (§1.1) | Enum-модели + CHECK в БД | test_roles_fixed_not_free_text |

## Явно отнесено к следующим частям

- Обработчики inbox/домен диалога (часть 2: T06-worker, T07, T08, T10).
- AI-адаптеры и токенизатор провайдера (часть 3: T11); до подключения
  заполненного профиля AI-вызовы заблокированы (ai_enabled=false).
- Голос, STT/TTS, аудио-каталог и sweep (часть 4: T15–T17).
- Отчёт: генерация, форматтер частей, доставка, consumed-credit (часть 4: T13–T14).
- Удаление, истечение, лимиты, бюджет, метрики, развёртывание (часть 5: T18–T24).
- Коммерческий контур Stars (P01–P06) — после функционального MVP.
- Проверка фактических доступов T01 и встреча передачи 5.3 — действия владельца
  (реестр smoke-проверок: provider-profile `smoke.*`, access-register в архиве ТЗ).
