# Чеклист соответствия части 2 требованиям ТЗ 2.0

Формат: требование ТЗ (раздел) → где реализовано → чем проверено.

## T06 (worker-часть) — inbox-обработчик и дедупликация

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Транзакционный обработчик inbox, ≤5 повторов, без повторения сохранённых эффектов (§3.2) | `app/workers/inbox_worker.py` (claim → фаза 1 → транзакция → mark_processed; requeue c backoff) | test_reprocessing_same_update…, test_handler_crash_retries… | готово |
| Повтор update_id подтверждается без повторного выполнения команды (§2.4) | webhook-дедупликация (часть 1) + идемпотентные unique_key экранов/заданий | test_reprocessing…, test_edited_like_repeat_does_not_overwrite_answer | готово |
| Доменная ошибка не коммутирует частичные эффекты (§2.2) | откат транзакции при DomainError, уведомление в новой транзакции (`notify_domain_error`) | test_no_credit_message_when_trial_used (сессия не создаётся), test_start_without_consent | готово |
| Просроченный lease возвращается в очередь, номер попытки растёт (§3.2) | `release_expired_leases` + периодический recovery в цикле | test_expired_lease_returns_to_queue (часть 1) | готово |
| answerCallbackQuery быстро, без ожидания AI (§3.1) | фаза 1 `pre_process` вне транзакции | test-сценарии кликов (bot.acks) | готово |
| Личный чат; в группе предложить личный чат, ответы не сохранять (§1.3) | `_handle_message`: chat.type != private → уведомление только на /start, профиль не создаётся | test_group_start_suggests_private_chat, test_group_plain_text_ignored | готово |

## T07 — Вход, согласие, выбор параметров (§3.7)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| /start → согласие → роль → уровень → формат → сводка | `app/services/ui_flow.py`, `_cmd_start`, `_route_action` | test_full_start_creates_session…, test_selection_navigation_and_back | готово |
| На входе объяснить длительность (6 вопросов, ≤2 уточнений, оба формата, один бесплатный разбор) | messages.GREETING | test_start_renders_greeting_and_consent | готово |
| Уведомление об AI-обработке, сроках хранения, способе удаления | messages.CONSENT_NOTICE (версия документа 2.0) | test_start_renders_greeting_and_consent | готово |
| Кнопка согласия записывает версию и дату; отказ блокирует интервью, /help остаётся | `set_consent`, ui_state consent_declined | test_consent_records_version_date…, test_consent_decline_blocks_interview_keeps_help | готово |
| Trial-credit создаётся при первом согласованном входе, UNIQUE(user_id) (§3.8) | `ensure_trial_credit` (ON CONFLICT по частичному индексу) | test_consent_records_version_date_and_trial_credit, test_reprocessing (не дублируется) | готово |
| Выбор отдельными сообщениями с inline-кнопками и возвратом | render_role/level/format/summary + кнопки «Назад» | test_selection_navigation_and_back | готово |
| Сводка фиксирует параметры; «Начать» расходует резерв, не попытку | render_summary + start_session_tx (reserved) | test_selection…, test_full_start (credits[0].state == reserved) | готово |
| Возврат к выбору до старта не расходует попытку | резерв только в start_session_tx | test_selection_navigation_and_back (credit остаётся available) | готово |
| UI-селекция одноразовым токеном: 16 байт base64url = 22 символа; срок выбора 30 мин, удаления/отмены 5 мин, управления вопросом 24 ч (§3.1) | `ui_tokens_repo`, TTL из settings.ui_tokens | test_token_* (часть 1), test_expired_token_shows_actual_screen | готово |
| Старые кнопки не управляют новой сессией; после устаревания — актуальный экран (§2.4, §3.1) | revoke_unconsumed при перерисовке; consume → None → STALE_BUTTON + render_current_screen; ui_state хранит незавершённый выбор | test_old_selection_button_after_navigation, test_token_single_use_second_click_stale | готово |
| Callback привязан к владельцу (§2.2) | consume проверяет telegram_user_id | test_foreign_owner_click_ignored | готово |
| Кампания-код только из allowlist, без персональных данных (§3.7) | `_create_profile` | test_campaign_code_only_from_allowlist | готово |
| Неизвестное значение параметра отклоняется; специализированные UNSUPPORTED_* (§1.3, §3.1) | `_parse_enum`/`_parse_triplet` | test_contracts_examples (часть 1), код в `_route_action` | готово |

## T08 — Старт сессии и резерв попытки (§2.2, §3.1, §3.8)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Транзакция: блокировка пользователя, лимиты, резерв одной доступной попытки, сессия и первый вопрос атомарно (§2.2) | `start_session_tx` (lock users → sessions guard → лимиты → create session → reserve → turn → outbox) | test_full_start_creates_session…, test_active_session_blocks_second_start | готово |
| Порядок блокировок users → sessions → credits (§2.3) | все транзакции диспетчера начинаются с lock_by_telegram_id | обзор кода; гонки в test_active_session | готово |
| Выбор попытки trial → grant → payment (§3.8) | `reserve_available` ORDER BY CASE source | test_full_start (trial reserved); схема (часть 1) | готово |
| Одна активная сессия; ACTIVE_SESSION (§1.3, §3.2) | find_active for_update + частичный UNIQUE-индекс | test_active_session_blocks_second_start | готово |
| 3 старта за скользящие 24 ч; отменённый и неудачный старт учитываются (§1.3) | count_starts_since по created_at всех статусов → RATE_LIMITED c retry_after | test_starts_per_24h_limit_counts_cancelled | готово |
| 10 команд в минуту → RATE_LIMITED с корректным retry_after (§1.3, §3.2) | `_check_rate_limit` по inbox(user_id, received_at) | test_commands_rate_limit | готово |
| NO_CREDIT: показать продолжение/завершение/доступ; при выключенной оплате — сообщение о завершении пробного доступа (§3.2, §3.7) | NO_CREDIT_BILLING_OFF | test_no_credit_message_when_trial_used | готово |
| Версии каталога/промпта/профиля фиксируются при старте (§3.6) | sessions.create(catalog_version, prompt_version, model_profile_version) | test_full_start (catalog_version == 2.0) | готово |
| Первый вопрос из каталога без ожидания LLM (§2.2) | turn 1 = self_introduction сценария; вопрос ≤700 | test_full_start, test_all_six_role_level_combinations_start | готово |
| AT01: все 6 комбинаций роли/уровня создают сессию с первым вопросом правильной темы | start_session_tx для всех сценариев | test_all_six_role_level_combinations_start | готово (формат voice — вопрос текстом, озвучка в части 4) |
| Сессия preparing → delivering_question → awaiting_answer после подтверждения Bot API (§2.4) | transition + хук `on_outbox_delivered_tx` | test_full_start (status awaiting_answer, delivered_message_id) | готово |
| Исчерпание доставки → failed; резерв освобождается (§2.4) | `on_outbox_failed_tx` | test_terminal_failure_fails_session_and_releases_credit | готово |
| Событие start с дедупликацией по бизнес-переходу (§3.10) | events_repo.emit(dedup_key=start:{session_id}) | test_full_start | готово |

## T09 (транзакционная половина) — приём текста (§2.2, §3.3)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Reply принадлежит текущему вопросу: подтверждённый message_id или известная копия (§2.4) | `accept_text_answer_tx`: known_ids = delivered_message_id ∪ receipts | test_answer_requires_reply_to_current_question | готово |
| Нормализация и длина до создания задания; пусто → EMPTY_ANSWER; 4001 → ANSWER_TOO_LONG; не обрезать (§3.3) | `app/domain/answers.py` (часть 1) + порядок вызовов | test_empty_and_overlong_answers_rejected, test_boundary_4000_chars_accepted | готово |
| Ответ + версия сессии + уникальное assess-задание + подтверждение — одна транзакция (§2.2) | accept_text_answer_tx | test_answer_accepted_atomically | готово |
| «Ответ принят. Готовлю следующий вопрос.» сразу после сохранения (§3.7) | ack через outbox reply на ответ | test_answer_accepted_atomically | готово |
| Двойной ответ → BUSY, без нового job, без перезаписи (§2.4, AT05) | save_answer guard + transition guard | test_second_answer_returns_busy_without_new_job | готово |
| Отредактированные сообщения не изменяют принятые ответы (§2.4) | edited_message — unsupported-вид (skipped на webhook, часть 1) | test_unsupported_kind_skipped (часть 1), test_edited_like_repeat… | готово |
| События first_answer / answer_accepted (§3.10) | events с dedup_key | test_answer_accepted_atomically | готово |
| Ответ без Reply → REPLY_REQUIRED «ответьте на последний вопрос» (§3.2) | guard | test_answer_requires_reply_to_current_question | готово |
| Пока evaluating второй ответ не принимается, статус и отмена доступны (§3.7) | статус-guard BUSY; кнопки управления остаются | test_second_answer…, test_finish_during_evaluation | готово |
| Голос в text-формате отклоняется с предложением текста; смена формата только в новой сессии (§3.5) | VOICE_IN_TEXT_FORMAT | test_voice_in_text_session_rejected | готово |
| В voice-формате можно ответить текстом (§3.5) | accept_text_answer_tx не зависит от формата | test_answer_accepted (text) + voice-сессия стартует | готово (STT — часть 4) |

## T10 — Worker lease и sender outbox (§1.4, §2.4, §3.2)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Восстановление после перезапуска: просроченный lease → очередь (§3.2) | inbox: release_expired_leases; outbox: CTE recovered (sending c истёкшим дедлайном → pending) | test_expired_lease…; логика claim_next | готово |
| 1 отправка/с на чат, ≤20/с суммарно (§1.4) | RateLimiter (global + per-chat) | юнит-логика; smoke | готово |
| 429 → retry_after (§1.4, §3.2) | SendOutcome.retry_after → schedule_retry(now+retry_after) | test_retry_after_respected | готово |
| ≤10 повторов в 24 ч с backoff (§3.2) | retry_deadline_exceeded(attempts/window) | test_terminal_failure… (10 сетевых отказов → failed) | готово |
| 403 чат недоступен / 400 ошибка реализации — не повторяются бесконечно (§3.2) | chat_unreachable/bad_request → терминально | test_chat_unreachable_no_notification (attempts==1), test_bad_request_is_terminal | готово |
| Порядок: следующий обязательный текст после подтверждения предыдущего; TTS не задерживает текст (§2.4) | claim_next: NOT EXISTS предшественника question_text/report_part | test_required_order_blocks_next_until_confirmed | готово |
| Неизвестный результат допускает повтор; receipts — только известные подтверждения (§2.4) | mark_delivered: receipts.message_ids; повтор при retryable | test_retry_after_respected | готово |
| При восстановлении не отправлять результат отменённой/удалённой сессии (§3.2) | _must_skip: deletion flag + cancelled-сессия (кроме служебных text) | test_cancelled_session_question_not_sent | готово |
| Уведомление о сбое доставки только при доступности чата (§3.2) | chat_reachable-параметр хука | test_chat_unreachable_no_notification | готово |
| message_id сохраняется после подтверждения Bot API (§2.2) | on_outbox_delivered_tx | test_full_start (delivered_message_id) | готово |
| ForceReply только у вопроса; управление отдельным сообщением [S1] (§3.7) | force_reply_payload + buttons_payload | test_full_start (has_force_reply; inline только в control) | готово |
| Классификация исходов Bot API: 429 retry_after / 403 / 400 / 401 / 5xx / сеть (§3.2) | `AiogramBotClient` (aiogram, опциональный api_base для локального Bot API-сервера) | test_bot_adapter (локальный стаб Bot API: полный стек aiogram→HTTP→маппинг) | готово |

## Управление сессией (§2.4, §3.1, §3.3, §3.7)

| Требование | Реализация | Проверка | Статус |
|---|---|---|---|
| Пропуск: answer NULL, оценки нет, без LLM, переход к следующей основной теме (§3.3) | skip_turn_tx + _advance_to_main | test_skip_advances_to_next_main_topic | готово |
| Все 6 пропущены → cancelled + освобождение попытки; «Попытка сохранена» (§3.3, §3.7) | _cancel_without_answers | test_skip_all_six_cancels_and_returns_credit | готово |
| Кнопка прежнего вопроса устаревает после перехода (§2.4) | токен привязан к turn_id (allowed_value) | test_skip_stale_turn_button | готово |
| «Завершить» без ответов → NO_ANSWERS, cancelled, резерв освобождён (§3.3) | finish_session_tx → _cancel_without_answers | test_finish_without_answers_cancels | готово |
| «Завершить» во время оценки → requested_action=finish; новая тема не создаётся (§2.4) | set_requested_action | test_finish_during_evaluation_records_requested_action | готово |
| Отмена требует отдельного подтверждения (§3.7); токен 5 мин | cancel_session → cancel_confirm/cancel_decline | test_cancel_requires_confirmation…, test_cancel_decline_keeps_session | готово |
| Отмена → cancelled + освобождение резерва; терминал не возобновляется (§2.4) | cancel_session_tx | test_cancel_requires_confirmation_and_releases_credit | готово |
| /status: активная сессия либо «Сейчас нет активной тренировки» (§3.7) | _cmd_status + build_status_text | test_status_empty_and_active | готово |
| /report: «У вас пока нет отчёта…» до первого интервью; сохранённый отчёт возвращается без нового LLM (§2.2, §3.7) | _cmd_report (+ REPORT_EXPIRED по сроку) | test_report_empty_state | готово (генерация отчёта — часть 4) |
| /balance: available, reserved, trial_used (§3.1) | _cmd_balance | test_balance_states | готово |
| /delete: явное предупреждение; подтверждение; блокировка новых ответов и покупок (§3.7) | render_delete_confirmation + request_delete_tx | test_delete_flow_blocks_new_actions, test_delete_declined | готово (физическое удаление/tombstone — часть 5, T18) |
| /help доступен в любой момент, в т.ч. без профиля и без согласия (§3.7) | _handle_message/_cmd_help + direct hint | test_help_available_anytime | готово |
| /buy до коммерческого этапа — оплата выключена (§3.8) | BILLING_DISABLED | test_unknown_command_and_billing_disabled | готово |
| Пустое состояние «нет сессии» для произвольного текста (§3.7) | _handle_plain_message | test_plain_text_without_session_guides | готово |
| Неподдерживаемое содержимое (фото/документы/ссылки) не обрабатывается (§1.3) | голос/контент-ветки; документы — вне домена | test_unsupported_content_in_private_chat | готово |
| Состояние error: конкретная причина и одно действие (§3.7) | _respond_domain_error + реестр сообщений §3.2 | все err-сценарии | готово |

## Live-проверка части 2

Реальный запуск двух процессов против локального PostgreSQL и api.telegram.org:
webhook 200 → inbox processed → профиль + 2 ui-токена + 2 строки outbox →
sender выполнил реальные вызовы sendMessage и корректно классифицировал 401
(фиктивный токен) как терминальный config_error без бесконечных повторов (§3.2).

## Явно отнесено к следующим частям

- AI-адаптер LLM, обработка assess-заданий, адаптация и уточнения (часть 3: T11, T12).
- Голос: STT, подтверждение расшифровки, TTS-озвучка вопросов (часть 4: T15–T17).
- Генерация и доставка отчёта, consumed-credit, рейтинг полезности (часть 4: T13–T14).
- Истечение сессии по неактивности, физическое удаление, tombstones, retention-sweeps (часть 5: T18).
- Лимиты AI-бюджета, метрики, сигналы оператору (часть 5: T19).
- Коммерческий контур Stars: /buy, invoice, pre_checkout, возвраты (P01–P06).
