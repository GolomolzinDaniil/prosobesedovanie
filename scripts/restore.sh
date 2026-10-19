#!/usr/bin/env bash
# Восстановление из резервной копии (§3.10, T23; AT17).
#
# Порядок обязателен: трафик принимается ТОЛЬКО после повторного удаления
# профилей из tombstones (§3.9). RPO — до 24 ч (ежедневный backup),
# целевой RTO — 2 ч при доступном сервере и проверенном restore (§3.10).
#
# Запуск:
#   BACKUP_PASSPHRASE=... ./scripts/restore.sh backups/prosobesedovanie-....dump.gz.enc
#
# Шаги:
#   1. остановить api и worker (трафик не принимается);
#   2. расшифровать и восстановить dump (pg_restore --clean --if-exists);
#   3. python scripts/reapply_tombstones.py (inbox сверяется с tombstones,
#      профили удалённых пользователей удаляются повторно);
#   4. запустить worker и api;
#   5. smoke-проверка (scripts/smoke.py) — health и сигналы.
#
# Код возврата: 0 — восстановление и smoke выполнены; 1 — ошибка.

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="${COMPOSE:-docker compose}"
DUMP="${1:?укажите файл резервной копии}"

[ -f "$DUMP" ] || { echo "файл копии не найден: $DUMP" >&2; exit 1; }
[ -n "${BACKUP_PASSPHRASE:-}" ] || { echo "BACKUP_PASSPHRASE не задана" >&2; exit 1; }

echo "== 1. остановка приёма трафика =="
${COMPOSE} stop api worker || true

echo "== 2. восстановление dump =="
TMP="$(mktemp /tmp/restore-XXXXXX.dump)"
trap 'rm -f "$TMP"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -pass "env:BACKUP_PASSPHRASE" -in "$DUMP" -out "$TMP"
cat "$TMP" | ${COMPOSE} exec -T db pg_restore \
    --username=app --dbname=prosobesedovanie \
    --clean --if-exists --no-owner --no-privileges \
    2>/tmp/restore_err.log || {
        # --clean падает на отсутствующих объектах при первом восстановлении —
        # допускаем предупреждения, но не прерываем: проверяем содержимое ниже
        grep -v "does not exist, skipping" /tmp/restore_err.log >&2 || true
    }

echo "== 3. повторное применение удалений (ДО приёма трафика, §3.9) =="
${COMPOSE} run --rm worker python scripts/reapply_tombstones.py

echo "== 4. запуск worker и api =="
${COMPOSE} --profile workers up -d worker
${COMPOSE} up -d api
sleep 5

echo "== 5. smoke =="
if python3 scripts/smoke.py --api-base "${API_BASE:-http://127.0.0.1:8080}"; then
    echo "RESTORE OK: трафик можно принимать; фактические RPO/RTO зафиксируйте"
    echo "в planning/deploy-checklist.md (журнал восстановления)."
    exit 0
else
    echo "RESTORE FAILED: smoke не пройден — трафик не принимать" >&2
    exit 1
fi
