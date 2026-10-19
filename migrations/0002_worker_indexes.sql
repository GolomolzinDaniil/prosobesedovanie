-- 0002_worker_indexes.sql — индексы для worker-нагрузки (часть 2).
-- Лимит команд (10/мин) считается по inbox; лимит стартов (3/24ч) — по
-- sessions.created_at; выбор available-credit — по порядку источника (§3.8).

-- Лимит команд в минуту: подсчёт недавних обновлений пользователя (§1.3)
CREATE INDEX IF NOT EXISTS inbox_user_recent ON inbox (user_id, received_at)
    WHERE user_id IS NOT NULL;

-- Лимит стартов за скользящие 24 часа (§1.3) и история сессий пользователя
CREATE INDEX IF NOT EXISTS sessions_user_created ON sessions (user_id, created_at DESC);

-- Выбор доступной попытки: trial -> grant -> payment (§3.8)
CREATE INDEX IF NOT EXISTS credits_available_selection ON credits (user_id, created_at)
    WHERE state = 'available';

-- Sender: выборка pending-строк и возврат зависших 'sending' после сбоя
-- (next_attempt_at в состоянии sending — дедлайн попытки отправки)
CREATE INDEX IF NOT EXISTS outbox_pending_attempt ON outbox (next_attempt_at)
    WHERE state IN ('pending', 'sending');

-- Расширение перечня действий ui_tokens: вход/согласие и экраны
-- подтверждения (часть 2, §3.7)
ALTER TABLE ui_tokens DROP CONSTRAINT ui_tokens_action_known;
ALTER TABLE ui_tokens ADD CONSTRAINT ui_tokens_action_known CHECK (action IN (
    'consent_accept', 'consent_decline',
    'select_role', 'select_level', 'select_format',
    'back_to_role', 'back_to_level', 'back_to_format',
    'start_session',
    'skip_turn', 'finish_session', 'cancel_session', 'cancel_confirm', 'cancel_decline',
    'confirm_delete', 'delete_decline',
    'confirm_transcript', 'edit_transcript', 'rerecord_voice',
    'report_rating_useful', 'report_rating_not_useful', 'new_training'
));
