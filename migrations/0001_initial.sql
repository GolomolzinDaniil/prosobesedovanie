-- 0001_initial.sql — Модель данных «ПроСобеседование» версии 2.0 (§2.3).
-- 14 таблиц: users, sessions, turns, reports, credits, inbox, jobs, outbox,
-- ui_tokens, events, daily_usage, ai_calls, deletion_tombstones, payments.
-- UUID генерируются сервером (gen_random_uuid, PG13+); время — timestamptz UTC.
-- Перечисления реализованы CHECK-ограничениями; правила связи — FK, UNIQUE,
-- CHECK и транзакционные проверки домена. JSONB валидируется моделями 2.0 до записи.
-- Таблица payments создаётся пустой в базовой схеме для FK credits; операции и
-- данные оплаты появляются только в коммерческом этапе.

-- ---------------------------------------------------------------------------
-- 1. users: один человек, только личный чат с ботом (§1.3)
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id      BIGINT      NOT NULL,
    chat_id               BIGINT      NOT NULL,
    ui_state              TEXT        NOT NULL DEFAULT 'new',
    version               INTEGER     NOT NULL DEFAULT 1,
    consent_version       TEXT,
    consent_at            TIMESTAMPTZ,
    source_code           TEXT,
    deletion_requested_at TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_telegram_id_unique UNIQUE (telegram_user_id),
    CONSTRAINT users_version_positive CHECK (version >= 1),
    -- пара consent_version и consent_version/consent_at NULL до согласия (§2.3)
    CONSTRAINT users_consent_pair CHECK ((consent_version IS NULL) = (consent_at IS NULL)),
    CONSTRAINT users_deletion_order CHECK (
        deletion_requested_at IS NULL OR deletion_requested_at >= created_at
    )
);
COMMENT ON TABLE users IS
    'Профиль: Telegram ID, личный chat ID, UI-состояние и версия обязательны; согласие — пара consent_version+consent_at; source_code из allowlist; признак запроса удаления.';

-- ---------------------------------------------------------------------------
-- 2. payments: коммерческий этап (§3.8); создаётся пустой для FK credits
-- ---------------------------------------------------------------------------
CREATE TABLE payments (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID REFERENCES users (id) ON DELETE SET NULL,
    product_id       TEXT        NOT NULL,
    units            INTEGER     NOT NULL,
    xtr_amount       INTEGER     NOT NULL,
    invoice_payload  TEXT        NOT NULL,
    charge_id        TEXT,
    status           TEXT        NOT NULL DEFAULT 'pending',
    terms_version    TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    paid_at          TIMESTAMPTZ,
    refund_requested_at TIMESTAMPTZ,
    refunded_at      TIMESTAMPTZ,
    CONSTRAINT payments_product_known CHECK (product_id IN ('single', 'pack5', 'pack10')),
    CONSTRAINT payments_units_match_product CHECK (
        (product_id = 'single' AND units = 1)
        OR (product_id = 'pack5' AND units = 5)
        OR (product_id = 'pack10' AND units = 10)
    ),
    CONSTRAINT payments_xtr_positive CHECK (xtr_amount > 0),
    CONSTRAINT payments_invoice_payload_unique UNIQUE (invoice_payload),
    -- charge_id NULL до оплаты; UNIQUE защищает от повторного начисления (§3.8)
    CONSTRAINT payments_charge_id_unique UNIQUE (charge_id),
    CONSTRAINT payments_status_known CHECK (
        status IN ('pending', 'paid', 'refund_pending', 'refunded', 'expired', 'reconciliation')
    ),
    CONSTRAINT payments_charge_after_payment CHECK (
        status IN ('pending', 'expired') OR charge_id IS NOT NULL
    ),
    CONSTRAINT payments_paid_time CHECK (
        status IN ('pending', 'expired') OR paid_at IS NOT NULL
    ),
    CONSTRAINT payments_refund_time CHECK (
        status <> 'refunded' OR refunded_at IS NOT NULL
    )
);
COMMENT ON TABLE payments IS
    'Только коммерческий этап: замороженные продукт, XTR-сумма, число единиц; charge_id NULL до оплаты; user_id может стать NULL после удаления профиля.';

-- ---------------------------------------------------------------------------
-- 3. sessions: одна активная сессия на пользователя (§2.3, §2.4)
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role_id                TEXT        NOT NULL,
    level                  TEXT        NOT NULL,
    format                 TEXT        NOT NULL,
    status                 TEXT        NOT NULL DEFAULT 'preparing',
    version                INTEGER     NOT NULL DEFAULT 1,
    requested_action       TEXT,
    catalog_version        TEXT        NOT NULL,
    prompt_version         TEXT        NOT NULL,
    -- NULL, если профиль провайдера не зафиксирован (AI-вызовы заблокированы)
    model_profile_version  TEXT,
    current_turn_id        UUID,
    current_difficulty     INTEGER     NOT NULL,
    follow_ups_used        INTEGER     NOT NULL DEFAULT 0,
    last_user_action_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at             TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ,
    CONSTRAINT sessions_role_known CHECK (role_id IN ('python', 'analyst', 'qa')),
    CONSTRAINT sessions_level_known CHECK (level IN ('intern', 'junior')),
    CONSTRAINT sessions_format_known CHECK (format IN ('text', 'voice')),
    CONSTRAINT sessions_status_known CHECK (
        status IN ('preparing', 'delivering_question', 'awaiting_answer', 'transcribing',
                   'confirming_transcript', 'evaluating', 'reporting',
                   'completed', 'cancelled', 'expired', 'failed')
    ),
    CONSTRAINT sessions_version_positive CHECK (version >= 1),
    CONSTRAINT sessions_requested_action_known CHECK (
        requested_action IS NULL OR requested_action IN ('finish', 'cancel')
    ),
    CONSTRAINT sessions_difficulty_range CHECK (current_difficulty BETWEEN 1 AND 3),
    CONSTRAINT sessions_follow_ups_limit CHECK (follow_ups_used BETWEEN 0 AND 2),
    -- finished_at нужен в терминальном статусе (§2.3)
    CONSTRAINT sessions_finished_required CHECK (
        status NOT IN ('completed', 'cancelled', 'expired', 'failed')
        OR finished_at IS NOT NULL
    ),
    CONSTRAINT sessions_expiry_order CHECK (expires_at >= created_at)
);
-- одна активная сессия на пользователя (§1.3): частичный уникальный индекс
CREATE UNIQUE INDEX sessions_one_active_per_user ON sessions (user_id)
    WHERE status NOT IN ('completed', 'cancelled', 'expired', 'failed');
CREATE INDEX sessions_expiry_sweep ON sessions (status, expires_at);
COMMENT ON TABLE sessions IS
    'Роль, уровень, формат, статус, версия, версии каталога/промпта/профиля модели; current_turn_id NULL до первого вопроса; finished_at обязателен в терминальном статусе.';

-- ---------------------------------------------------------------------------
-- 4. turns: до 8 на сессию; уточнение ссылается на основной вопрос той же
--    сессии; составной FK (turn_id, session_id) (§2.3)
-- ---------------------------------------------------------------------------
CREATE TABLE turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    ordinal             INTEGER     NOT NULL,
    kind                TEXT        NOT NULL,
    question_type       TEXT        NOT NULL,
    main_index          INTEGER,
    parent_turn_id      UUID,
    topic               TEXT        NOT NULL,
    question_text       TEXT        NOT NULL,
    rubric              JSONB       NOT NULL,
    difficulty          INTEGER,
    catalog_entry_id    TEXT        NOT NULL,
    answer_text         TEXT,
    answer_received_at  TIMESTAMPTZ,
    skipped             BOOLEAN     NOT NULL DEFAULT FALSE,
    assessment          JSONB,
    assessed_at         TIMESTAMPTZ,
    transcript_version  INTEGER,
    draft_transcript    TEXT,
    stt_attempts        INTEGER     NOT NULL DEFAULT 0,
    delivered_message_id BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT turns_session_ordinal_unique UNIQUE (session_id, ordinal),
    -- опора для составных FK (turn_id, session_id) из sessions/jobs
    CONSTRAINT turns_id_session_unique UNIQUE (id, session_id),
    CONSTRAINT turns_ordinal_positive CHECK (ordinal >= 1),
    CONSTRAINT turns_kind_known CHECK (kind IN ('main', 'follow_up')),
    CONSTRAINT turns_question_type_known CHECK (question_type IN ('hr', 'technical')),
    -- UNIQUE(session_id, main_index) для kind=main — частичным индексом ниже
    CONSTRAINT turns_main_index_range CHECK (main_index IS NULL OR main_index BETWEEN 1 AND 6),
    CONSTRAINT turns_main_index_pair CHECK ((kind = 'main') = (main_index IS NOT NULL)),
    -- parent_turn_id нужен только уточнению (§2.3)
    CONSTRAINT turns_parent_pair CHECK ((kind = 'follow_up') = (parent_turn_id IS NOT NULL)),
    CONSTRAINT turns_question_length CHECK (char_length(question_text) BETWEEN 1 AND 700),
    -- ответ 1..4000 кодовых точек после нормализации; при пропуске ответ NULL
    CONSTRAINT turns_answer_length CHECK (
        answer_text IS NULL OR char_length(answer_text) BETWEEN 1 AND 4000
    ),
    CONSTRAINT turns_skipped_consistency CHECK (
        skipped = FALSE OR (answer_text IS NULL AND assessment IS NULL)
    ),
    CONSTRAINT turns_answer_time CHECK (
        answer_text IS NULL OR answer_received_at IS NOT NULL
    ),
    CONSTRAINT turns_assessment_object CHECK (
        assessment IS NULL OR jsonb_typeof(assessment) = 'object'
    ),
    CONSTRAINT turns_rubric_object CHECK (jsonb_typeof(rubric) = 'object'),
    -- сложность обязательна для технических вопросов
    CONSTRAINT turns_technical_difficulty CHECK (
        question_type <> 'technical' OR difficulty IS NOT NULL
    ),
    CONSTRAINT turns_difficulty_range CHECK (difficulty IS NULL OR difficulty BETWEEN 1 AND 3),
    CONSTRAINT turns_transcript_version_range CHECK (
        transcript_version IS NULL OR transcript_version >= 1
    ),
    CONSTRAINT turns_draft_length CHECK (
        draft_transcript IS NULL OR char_length(draft_transcript) <= 4000
    ),
    -- максимум три запуска распознавания на один вопрос (§3.5)
    CONSTRAINT turns_stt_attempts_limit CHECK (stt_attempts BETWEEN 0 AND 3)
);
-- единственность основных вопросов (§2.3)
CREATE UNIQUE INDEX turns_one_main_per_index ON turns (session_id, main_index)
    WHERE kind = 'main';
-- максимум одно уточнение на parent_turn_id (§2.3)
CREATE UNIQUE INDEX turns_one_follow_up_per_parent ON turns (parent_turn_id)
    WHERE kind = 'follow_up';
-- уточнение ссылается на вопрос той же сессии (составной FK, §2.3)
ALTER TABLE turns
    ADD CONSTRAINT turns_parent_same_session
    FOREIGN KEY (parent_turn_id, session_id) REFERENCES turns (id, session_id)
    DEFERRABLE INITIALLY DEFERRED;

-- current_turn_id имеет ту же составную проверку (§2.3)
ALTER TABLE sessions
    ADD CONSTRAINT sessions_current_turn_same_session
    FOREIGN KEY (current_turn_id, id) REFERENCES turns (id, session_id)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON TABLE turns IS
    'Порядок, тема, вопрос, рубрика и сложность; parent_turn_id только у уточнения; ответ и оценка NULL до получения; при пропуске ответ NULL.';

-- ---------------------------------------------------------------------------
-- 5. credits: права на попытки; trial UNIQUE на пользователя (§3.8)
-- ---------------------------------------------------------------------------
CREATE TABLE credits (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source               TEXT        NOT NULL,
    state                TEXT        NOT NULL DEFAULT 'available',
    payment_id           UUID REFERENCES payments (id) ON DELETE RESTRICT,
    unit_index           INTEGER,
    grant_reason         TEXT,
    reserved_session_id  UUID REFERENCES sessions (id) ON DELETE SET NULL,
    consumed_session_id  UUID REFERENCES sessions (id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT credits_source_known CHECK (source IN ('trial', 'grant', 'payment')),
    CONSTRAINT credits_state_known CHECK (
        state IN ('available', 'reserved', 'consumed', 'revoked')
    ),
    -- payment_id и unit_index нужны для платной попытки (§2.3)
    CONSTRAINT credits_payment_fields_pair CHECK (
        (source = 'payment') = (payment_id IS NOT NULL AND unit_index IS NOT NULL)
    ),
    CONSTRAINT credits_unit_index_positive CHECK (
        unit_index IS NULL OR unit_index >= 1
    ),
    CONSTRAINT credits_units_unique_per_payment UNIQUE (payment_id, unit_index),
    CONSTRAINT credits_grant_reason_pair CHECK (
        (source = 'grant') = (grant_reason IS NOT NULL)
    ),
    -- reserved_session_id только в reserved, consumed_session_id только в consumed
    CONSTRAINT credits_reserved_pair CHECK (
        (state = 'reserved') = (reserved_session_id IS NOT NULL)
    ),
    CONSTRAINT credits_consumed_pair CHECK (
        (state = 'consumed') = (consumed_session_id IS NOT NULL)
    ),
    CONSTRAINT credits_available_clean CHECK (
        state <> 'available'
        OR (reserved_session_id IS NULL AND consumed_session_id IS NULL)
    )
);
-- одна trial-попытка на пользователя (§3.8)
CREATE UNIQUE INDEX credits_one_trial_per_user ON credits (user_id)
    WHERE source = 'trial';
CREATE INDEX credits_available_by_user ON credits (user_id, state);
COMMENT ON TABLE credits IS
    'Источник и состояние; payment_id+unit_index для платной попытки; reserved_session_id только в reserved; consumed_session_id только в consumed.';

-- ---------------------------------------------------------------------------
-- 6. reports: не более одного на сессию (§2.3)
-- ---------------------------------------------------------------------------
CREATE TABLE reports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    report_json         JSONB       NOT NULL,
    parts               JSONB       NOT NULL,
    partial             BOOLEAN     NOT NULL,
    degraded            BOOLEAN     NOT NULL DEFAULT FALSE,
    total_chars         INTEGER     NOT NULL,
    delivery_started_at TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reports_session_unique UNIQUE (session_id),
    CONSTRAINT reports_report_object CHECK (jsonb_typeof(report_json) = 'object'),
    -- 1..4 текстовых сообщения по <=3500 (§1.3); содержимое частей проверяет app
    CONSTRAINT reports_parts_array CHECK (
        jsonb_typeof(parts) = 'array'
        AND jsonb_array_length(parts) BETWEEN 1 AND 4
    ),
    CONSTRAINT reports_total_chars_range CHECK (total_chars BETWEEN 1 AND 12000),
    CONSTRAINT reports_delivery_order CHECK (
        delivered_at IS NULL OR delivery_started_at IS NOT NULL
    )
);
COMMENT ON TABLE reports IS
    'JSON, отформатированные части, partial и degraded; delivery_started_at и delivered_at NULL до соответствующего события.';

-- ---------------------------------------------------------------------------
-- 7. inbox: надёжная очередь обновлений с дедупликацией по update_id (§2.2)
-- ---------------------------------------------------------------------------
CREATE TABLE inbox (
    update_id        BIGINT PRIMARY KEY,
    kind             TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    attempts         INTEGER     NOT NULL DEFAULT 0,
    user_id          UUID REFERENCES users (id) ON DELETE SET NULL,
    payload          JSONB,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at  TIMESTAMPTZ,
    last_error       TEXT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ,
    payload_purged_at TIMESTAMPTZ,
    CONSTRAINT inbox_status_known CHECK (
        status IN ('pending', 'processing', 'processed', 'skipped', 'failed')
    ),
    CONSTRAINT inbox_attempts_non_negative CHECK (attempts >= 0),
    CONSTRAINT inbox_kind_short CHECK (char_length(kind) <= 64),
    CONSTRAINT inbox_processing_lease CHECK (
        status <> 'processing' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT inbox_terminal_time CHECK (
        status NOT IN ('processed', 'skipped', 'failed') OR processed_at IS NOT NULL
    ),
    -- payload удаляется после обработки (§3.9): очищенная строка помечается
    CONSTRAINT inbox_purge_mark CHECK (
        payload IS NOT NULL OR payload_purged_at IS NOT NULL OR status = 'pending'
    )
);
CREATE INDEX inbox_claimable ON inbox (received_at)
    WHERE status IN ('pending', 'processing');
COMMENT ON TABLE inbox IS
    'update_id PK (повтор получает 200 без повторной команды); статус, счётчики, время; payload обнуляется после обработки; user_id NULL допускает неизвестного пользователя.';

-- ---------------------------------------------------------------------------
-- 8. jobs: надёжные AI-задания; SELECT FOR UPDATE SKIP LOCKED + lease (§2.1)
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unique_key       TEXT        NOT NULL,
    session_id       UUID        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    turn_id          UUID,
    kind             TEXT        NOT NULL,
    api_version      TEXT        NOT NULL DEFAULT '2.0',
    state            TEXT        NOT NULL DEFAULT 'pending',
    attempts         INTEGER     NOT NULL DEFAULT 0,
    expected_version INTEGER     NOT NULL,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    payload          JSONB       NOT NULL,
    result           JSONB,
    media_storage_key TEXT,
    error_code       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    CONSTRAINT jobs_unique_key UNIQUE (unique_key),
    CONSTRAINT jobs_kind_known CHECK (kind IN ('assess', 'transcribe', 'report', 'tts')),
    CONSTRAINT jobs_state_known CHECK (
        state IN ('pending', 'leased', 'done', 'failed', 'cancelled')
    ),
    CONSTRAINT jobs_attempts_non_negative CHECK (attempts >= 0),
    CONSTRAINT jobs_expected_version_positive CHECK (expected_version >= 1),
    -- turn_id обязателен для assess/transcribe (§2.3)
    CONSTRAINT jobs_turn_required CHECK (
        kind NOT IN ('assess', 'transcribe') OR turn_id IS NOT NULL
    ),
    CONSTRAINT jobs_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT jobs_leased CHECK (
        state <> 'leased' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT jobs_terminal_time CHECK (
        state NOT IN ('done', 'failed', 'cancelled') OR finished_at IS NOT NULL
    ),
    CONSTRAINT jobs_terminal_result CHECK (
        state <> 'done' OR result IS NOT NULL
    )
);
-- turn той же сессии (составной FK, §2.3)
ALTER TABLE jobs
    ADD CONSTRAINT jobs_turn_same_session
    FOREIGN KEY (turn_id, session_id) REFERENCES turns (id, session_id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX jobs_claimable ON jobs (created_at) WHERE state = 'pending';
CREATE INDEX jobs_lease_sweep ON jobs (lease_expires_at) WHERE state = 'leased';
COMMENT ON TABLE jobs IS
    'Вид, версия, состояние, попытки и lease; unique_key UNIQUE (идемпотентное создание); expected_version для защиты от поздних результатов; media_storage_key временный.';

-- ---------------------------------------------------------------------------
-- 9. outbox: надёжная очередь исходящих сообщений (§2.2, §3.2)
-- ---------------------------------------------------------------------------
CREATE TABLE outbox (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unique_key          TEXT        NOT NULL,
    user_id             UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session_id          UUID REFERENCES sessions (id) ON DELETE CASCADE,
    chat_id             BIGINT      NOT NULL,
    kind                TEXT        NOT NULL,
    seq                 INTEGER     NOT NULL,
    is_required         BOOLEAN     NOT NULL DEFAULT TRUE,
    state               TEXT        NOT NULL DEFAULT 'pending',
    payload             JSONB,
    reply_to_message_id BIGINT,
    reply_to_outbox_id  UUID REFERENCES outbox (id) ON DELETE SET NULL,
    message_id          BIGINT,
    attempts            INTEGER     NOT NULL DEFAULT 0,
    receipts            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    next_attempt_at     TIMESTAMPTZ,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at        TIMESTAMPTZ,
    CONSTRAINT outbox_unique_key UNIQUE (unique_key),
    CONSTRAINT outbox_kind_known CHECK (
        kind IN ('question_text', 'question_voice', 'report_part', 'text', 'buttons')
    ),
    CONSTRAINT outbox_seq_positive CHECK (seq >= 1),
    -- порядок outbox: следующий обязательный текст не отправляется,
    -- пока предыдущий не подтверждён (§2.4)
    CONSTRAINT outbox_chat_seq_unique UNIQUE (chat_id, seq),
    CONSTRAINT outbox_state_known CHECK (
        state IN ('pending', 'sending', 'delivered', 'failed', 'cancelled')
    ),
    CONSTRAINT outbox_delivered_receipt CHECK (
        state <> 'delivered' OR (message_id IS NOT NULL AND delivered_at IS NOT NULL)
    ),
    CONSTRAINT outbox_receipts_object CHECK (jsonb_typeof(receipts) = 'object'),
    CONSTRAINT outbox_attempts_limit CHECK (attempts >= 0 AND attempts <= 10),
    -- payload обязателен в момент отправки; после доставки может быть обнулён
    -- политикой хранения (§3.9: обнулить payload до 24 часов после обработки)
    CONSTRAINT outbox_payload_present_when_sending CHECK (
        state <> 'sending' OR payload IS NOT NULL
    )
);
CREATE INDEX outbox_claimable ON outbox (chat_id, seq)
    WHERE state IN ('pending', 'sending');
COMMENT ON TABLE outbox IS
    'Вид, порядок (seq на чат), обязательность и доставка; receipts содержат только известные подтверждения; message_id сохраняется после подтверждения Bot API.';

-- ---------------------------------------------------------------------------
-- 10. ui_tokens: непрозрачные одноразовые callback-токены 22 символа (§3.1)
-- ---------------------------------------------------------------------------
CREATE TABLE ui_tokens (
    token            CHAR(22) PRIMARY KEY,
    user_id          UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    action           TEXT        NOT NULL,
    allowed_value    TEXT,
    session_id       UUID REFERENCES sessions (id) ON DELETE CASCADE,
    expected_version INTEGER,
    expires_at       TIMESTAMPTZ NOT NULL,
    consumed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ui_tokens_action_known CHECK (action IN (
        'select_role', 'select_level', 'select_format', 'back_to_level',
        'back_to_role', 'start_session', 'skip_turn', 'finish_session',
        'cancel_session', 'cancel_confirm', 'confirm_delete',
        'confirm_transcript', 'edit_transcript', 'rerecord_voice',
        'report_rating_useful', 'report_rating_not_useful', 'new_training'
    )),
    CONSTRAINT ui_tokens_consumed_once CHECK (
        consumed_at IS NULL OR consumed_at >= created_at
    ),
    CONSTRAINT ui_tokens_version_positive CHECK (
        expected_version IS NULL OR expected_version >= 1
    )
);
CREATE INDEX ui_tokens_active ON ui_tokens (user_id, action) WHERE consumed_at IS NULL;
COMMENT ON TABLE ui_tokens IS
    '16 случайных байт base64url без padding = char(22); действие, ограниченное значение, владелец, версия, срок; consumed_at NULL до одноразового использования.';

-- ---------------------------------------------------------------------------
-- 11. events: бизнес-события с дедупликацией (§3.10); ответы не хранятся
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dedup_key      TEXT        NOT NULL,
    name           TEXT        NOT NULL,
    user_id        UUID REFERENCES users (id) ON DELETE SET NULL,
    session_id     UUID REFERENCES sessions (id) ON DELETE SET NULL,
    props_numeric  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    props_category JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT events_dedup_unique UNIQUE (dedup_key),
    CONSTRAINT events_name_known CHECK (name IN (
        'start', 'first_answer', 'answer_accepted', 'report_delivered',
        'report_rating', 'trial_consumed',
        'invoice_created', 'payment_success', 'refund_success'
    )),
    -- ссылки обнуляются при удалении (§2.3)
    CONSTRAINT events_props_numeric_object CHECK (jsonb_typeof(props_numeric) = 'object'),
    CONSTRAINT events_props_category_object CHECK (jsonb_typeof(props_category) = 'object')
);
CREATE INDEX events_cleanup ON events (created_at);
COMMENT ON TABLE events IS
    'Имя и разрешённые числовые/категориальные свойства; дедупликация по бизнес-переходу через dedup_key; ответы не хранятся.';

-- ---------------------------------------------------------------------------
-- 12. daily_usage: суточный денежный учёт AI (§3.10)
-- ---------------------------------------------------------------------------
CREATE TABLE daily_usage (
    day          DATE PRIMARY KEY,
    reserved_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    known_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    unknown_usd  NUMERIC(14, 6) NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT daily_usage_non_negative CHECK (
        reserved_usd >= 0 AND known_usd >= 0 AND unknown_usd >= 0
    )
);
COMMENT ON TABLE daily_usage IS
    'Денежный резерв, известные расходы и неизвестные списания; неотрицательные decimal; суточный лимит блокирует новые дорогие задания.';

-- ---------------------------------------------------------------------------
-- 13. ai_calls: журнал попыток и стоимости (§3.10); содержимое запросов не хранится
-- ---------------------------------------------------------------------------
CREATE TABLE ai_calls (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                UUID        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt               INTEGER     NOT NULL,
    operation             TEXT        NOT NULL,
    provider              TEXT        NOT NULL,
    model                 TEXT        NOT NULL,
    profile_version       TEXT        NOT NULL,
    input_tokens_estimated  INTEGER,
    output_tokens_estimated INTEGER,
    reserved_usd          NUMERIC(14, 6) NOT NULL,
    actual_usd            NUMERIC(14, 6),
    result_state          TEXT        NOT NULL DEFAULT 'unknown',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ai_calls_job_attempt_unique UNIQUE (job_id, attempt),
    CONSTRAINT ai_calls_attempt_positive CHECK (attempt >= 1),
    CONSTRAINT ai_calls_operation_known CHECK (
        operation IN ('assess', 'report', 'stt', 'tts')
    ),
    CONSTRAINT ai_calls_tokens_non_negative CHECK (
        (input_tokens_estimated IS NULL OR input_tokens_estimated >= 0)
        AND (output_tokens_estimated IS NULL OR output_tokens_estimated >= 0)
    ),
    CONSTRAINT ai_calls_reserved_non_negative CHECK (reserved_usd >= 0),
    CONSTRAINT ai_calls_actual_non_negative CHECK (
        actual_usd IS NULL OR actual_usd >= 0
    ),
    CONSTRAINT ai_calls_result_state_known CHECK (
        result_state IN ('success', 'known_failure', 'unknown')
    ),
    -- успех заменяет резерв фактической стоимостью; известный нетарифицированный
    -- отказ освобождает его (0); неизвестный результат остаётся NULL до сверки (§3.10)
    CONSTRAINT ai_calls_actual_on_success CHECK (
        result_state <> 'success' OR actual_usd IS NOT NULL
    ),
    CONSTRAINT ai_calls_known_failure_zero CHECK (
        result_state <> 'known_failure' OR actual_usd = 0
    )
);
CREATE INDEX ai_calls_cleanup ON ai_calls (created_at);
COMMENT ON TABLE ai_calls IS
    'Операция, профиль, резерв стоимости; фактическая стоимость NULL при неизвестном результате; содержимое AI-запроса не хранится.';

-- ---------------------------------------------------------------------------
-- 14. deletion_tombstones: повторное применение удалений после restore (§3.9)
-- ---------------------------------------------------------------------------
CREATE TABLE deletion_tombstones (
    user_id    UUID PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT tombstone_expires_after CHECK (expires_at > deleted_at)
);
COMMENT ON TABLE deletion_tombstones IS
    'Только прежний внутренний UUID, без Telegram ID; хранится отдельно от циклических backup 7 дней; после restore удаления повторяются.';
