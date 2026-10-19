# Чеклист соответствия части 3 требованиям ТЗ 2.0

Формат: требование ТЗ (раздел) → где реализовано → чем проверено.
Задачи: T11, T12 (+ завершение T09-механики; эталоны T21 требуют предметного
рецензента и в часы разработчика не входят).

## T11 — AI-адаптер и валидация контрактов

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Провайдер через адаптер; без заполненного профиля вызовы блокируются (§1.4) | `app/providers/llm.py` (порт LlmClient + HttpLlmClient); `build_llm_client` возвращает None при ai_enabled=false | test_ai_disabled_profile_falls_back | готово |
| OpenAI-совместимый вызов: model, messages (system/user), max_tokens, temperature 0.2 (§3.3, §3.6) | HttpLlmClient.complete; ответ пользователя только в user message | test_success_returns_content_and_usage (assert по форме запроса) | готово |
| Таймаут одного вызова: оценка 20 с (§3.2) | LlmRequest.timeout_seconds из settings | test_llm_input_contains_envelope_contract | готово |
| Классификация ошибок: 429 (retry_after), 5xx/сеть — повтор; 400 — фатально; 401/403 — сигнал оператору, не повторяется (§3.2) | LlmRetryableError / LlmFatalError / LlmConfigError / LlmUnknownResultError | test_llm_adapter (локальный стаб), test_config_error_no_retry_and_fallback, test_retryable_error_then_success | готово |
| Не более 2 вызовов провайдера на логическую операцию, включая повтор после невалидного JSON и перезапуски задания (§3.2) | учёт `ai_calls UNIQUE(job_id, attempt)`; проверка count перед каждым вызовом | test_invalid_json_twice…, test_provider_calls_capped_at_two_across_reruns | готово |
| Повтор с паузой 2–5 с; retry_after учитывается только если укладывается в дедлайн (§3.2) | `_retry_wait` (pause из settings, дедлайн 110 с) | test_retryable_error_then_success (паузы обнулены в тест-конфиге) | готово |
| Неизвестный результат может быть тарифицирован: резерв не освобождается автоматически (§3.2) | LlmUnknownResultError → settle 'unknown' → unknown_usd | test_unknown_result_moves_reserve_to_unknown | готово |
| Строгая валидация JSON: один объект, схема TurnAssessment 2.0, неизвестные поля запрещены (§3.3, §3.4-подход) | parse_assessment_json + TurnAssessment (extra=forbid) | test_parse_* (unit), test_invalid_json_retry_then_success | готово |
| Семантическая валидация: цитаты буквально из ответа (NFC + пробелы), пары correctness/evidence, turn_id (§3.3) | validate_assessment_payload → contracts/semantic_validation | test_fabricated_quote_rejected, test_validate_payload_turn_id_mismatch | готово |
| Один повтор при невалидном выводе; если вновь невалиден — запасной сценарий (§3.3-подход, §3.2) | цикл ≤2 вызовов → AI_INVALID_OUTPUT | test_invalid_json_twice_falls_back_to_catalog | готово |
| Температура 0.2 при поддержке провайдера; эквивалент документируется в профиле (§3.3) | temperature_supported флаг профиля; strict_json_fallback_documented | test_success (temperature==0.2 в теле) | готово |
| AI-провайдер не обращается к таблицам и Telegram (§3.10) | адаптер получает только промпты, возвращает текст | обзор архитектуры | готово |
| Токенизатор провайдера проверяет объём до отправки (§1.3) | `estimate_tokens` (консервативная оценка) + подключение фактического токенизатора через профиль (tokenizer_reference) — при заполнении профиля | test_history_truncated (лимит 12000 соблюдается) | готово (эвристикой; фактический токенизатор — при выборе провайдера, T01) |

## Бюджет и стоимость (§1.3, §3.10)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Резерв стоимости транзакционно до каждого вызова по верхней границе (§3.10) | token_budget.reserve_call (ai_calls + daily_usage в одной транзакции) | test_success_cost_replaces_reserve | готово |
| Успех заменяет резерв фактической стоимостью; известный отказ освобождает; неизвестный → unknown_usd (§3.10) | settle_call success/known_failure/unknown | test_retryable_error_then_success (known_failure+success), test_unknown_result_moves_reserve_to_unknown | готово |
| Бюджет сессии 100000/22000 токенов включая повторы; резерв отчёта 24000/7000 сохраняется прежде всего (§1.3) | session_tokens_used + check_session_token_budget | test_session_token_budget_goes_to_reporting | готово |
| BUDGET_EXCEEDED: «Подготовим разбор уже полученных ответов»; не тратить резерв отчёта на следующий turn (§3.2) | переход в reporting + сообщение; LLM не вызывается | test_session_token_budget_goes_to_reporting | готово |
| Суточный лимит блокирует новые дорогие задания, сохраняя права пользователя и выдавая запасной результат (§3.10) | daily_budget_remaining → fallback без вызова; кредит остаётся reserved | test_daily_budget_blocks_expensive_call, test_session_token_budget (credits) | готово |
| Очередь AI до 60 секунд, затем запасной сценарий (§1.3) | проверка elapsed при claim + timeout семафора | test_queue_timeout_fallback_without_call | готово |
| 5 параллельных LLM (остальные в очереди); лимит глобальный, не на процесс (§1.3, §2.1) | asyncio.Semaphore(concurrent_llm) в JobsWorker | конфигурация; глобальный контроль при масштабировании — часть 5 (T19) | готово в пределах процесса |
| Журнал AI-затрат без содержимого запросов (§3.9, §3.10) | ai_calls: только токены/стоимость/провайдер/модель | схема (часть 1), test_assessment_applied (ai_calls) | готово |

## T12 — Адаптация и запасные переходы

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Jobs-worker: SKIP LOCKED + lease 120 с, heartbeat-продление каждые 20 с, просроченный lease → очередь, номер попытки растёт (§2.1, §3.2) | `jobs_worker.py` + jobs_repo.claim_next/renew_lease | test_provider_calls_capped (rerun), smoke-запуск воркера | готово |
| Два worker не применяют один результат: unique job, lease_owner и версия проверяются при commit (§3.2) | _phase_b: lock-цепочка + проверка lease_owner/state | test_cancelled_session_result_not_applied, код _phase_b | готово |
| expected_version: поздний результат устаревшей версии отбрасывается (§2.2, §2.4) | guard в _phase_b; finish синхронизирует версию открытого задания (requested_action не инвалидирует оценку) | test_late_result_of_stale_version_discarded, test_finish_during_evaluation_applies_then_reports | готово |
| Канонический порядок блокировок users → sessions → turns → jobs (§2.3) | _phase_b | обзор кода | готово |
| GenerateTurnInput: вопрос с рубрикой, ответ, предыдущие краткие оценки, следующая разрешённая тема, остаток уточнений (§3.3) | build_generate_turn_input + prompts/user-envelope | test_llm_input_contains_envelope_contract | готово |
| История в пределах 12000 токенов; сокращение по старым объяснениям, не по текущему ответу (§3.3) | цикл усечения в build_generate_turn_input | test_history_truncated_keeping_current_answer, test_previous_assessments_accumulate | готово |
| Фиксированное правило действия: relevance=0 или «не знаю» → дальше; уточнение при тематической ≤2, relevance≥1, missing_points, лимите и отсутствии уточнения у родителя (§3.3) | compute_allowed_action (часть 1) + _apply_assessment | test_dont_know_answer_skips_follow_up, test_relevance_zero_forces_next_topic, test_follow_up_created_on_weak_answer | готово |
| suggested_action модели принимается только если совпадает с допустимым; иначе приоритет сервера, next_question отбрасывается (§3.3) | model_opinion_accepted | test_dont_know (мнение отброшено), test_off_topic (вопрос отброшен) | готово |
| ≤1 уточнение на основной вопрос, ≤2 на сессию; уточнение — та же тема, сложность родителя (§1.3, §3.3) | _create_follow_up + guard-ы; UNIQUE(parent_turn_id) в БД | test_one_follow_up_per_parent, test_session_follow_up_limit_is_two, test_follow_up_created_on_weak_answer | готово |
| Шаг сложности: +1 при тематической ≥3 и relevance≥3; −1 при ≤1; иначе сохранить; intern 1–2, junior 1–3; HR не меняет; уточнение использует сложность родителя (§3.3) | compute_next_difficulty (часть 1) + set_difficulty | test_difficulty_steps_junior, test_difficulty_clamped_for_intern, test_hr_and_follow_up_do_not_change_difficulty | готово |
| Отклонить вопрос модели: пустой, >700, посторонняя тема, небезопасное требование → вопрос каталога (§3.3) | _validate_model_question + question_matches_topic (лексика рубрики каталога) | test_off_topic_model_question_replaced_by_catalog, test_invalid_follow_up_question_falls_back | готово (полная предметная проверка — T21 с рецензентом) |
| Валидный вопрос модели используется в разрешённой теме и границах уровня (§3.3) | question_override для technical | test_model_question_adopted_for_technical_topic | готово |
| AI_UNAVAILABLE/AI_INVALID_OUTPUT: «Интервью продолжается с ограниченной оценкой»; следующий вопрос из каталога; оценка недоступна (§3.2) | _apply_fallback: notice + advance_to_main каталогом, assessment=NULL | test_invalid_json_twice…, test_config_error…, test_queue_timeout…, test_ai_disabled… | готово |
| «Завершить» во время оценки: новая тема после окончания оценки не создаётся (§2.4) | requested_action guard в _apply_assessment → _goto_reporting | test_finish_during_evaluation_applies_then_reports | готово |
| По окончании плана (6 основных) — отчёт; уточнения не считаются основными темами (§3.3) | _goto_reporting при next_index > 6 | test_full_text_interview_reaches_reporting | готово (исполнение отчёта — часть 4) |
| Интерфейс показывает ожидание: после 15 с обновить существующий статус, максимум один раз (§3.7) | loading_update_task + bot.edit_message_text | test_loading_status_updated_once_after_threshold | готово |
| Финансовый резерв записывается до внешнего вызова (§3.2) | reserve_call перед llm.complete | test_unknown_result (2 резерва), test_success_cost | готово |
| Оценка сохраняется до подтверждения в чате; сбой не списывает вторую попытку и не принимает ответ повторно (§1.3) | фаза B транзакционна; crash → release → rerun без дублей (unique_key) | test_provider_calls_capped_at_two_across_reruns | готово |
| Отчёт помечается при необходимости (degraded) — оценка NULL в снимке (§3.4) | assessment остаётся NULL → SnapshotTurn.assessment=null | test_invalid_json_twice (assessment NULL); разметка degraded — часть 4 | готово (данные для части 4) |

## Открытые пункты (вне части 3)

- 18 предметных эталонов AT04/AT10 и проверка качества промптов — T21,
  требуется участие предметного рецензента (2–4 ч, вне часов разработчика).
- Фактический токенизатор и smoke выбранного провайдера — T01 (владелец).
- Исполнение report-заданий, голос — части 4–5.
