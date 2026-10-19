#!/usr/bin/env bash
# Ежедневный резервный dump (§3.10, T23; AT17).
#
# - pg_dump ролью backup (только чтение, §3.9) из контейнера db;
# - шифрование AES-256-CBC парольной фразой из окружения BACKUP_PASSPHRASE
#   (никогда не хранится в репозитории);
# - скользящие 7 дней: копии старее backup_rolling_days удаляются (§3.9);
# - статус пишётся в RUN_DIR worker-контейнера — монитор сигнала «ошибка
#   backup» читает его (§3.10).
#
# Запуск (cron на хосте, ежедневно):
#   BACKUP_PASSPHRASE=... ./scripts/backup.sh [каталог_назначения]
#
# Код возврата: 0 — копия создана; 1 — ошибка (статус ok=false записан).

set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${1:-${BACKUP_DIR:-./backups}}"
COMPOSE="${COMPOSE:-docker compose}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="${BACKUP_DIR}/prosobesedovanie-${STAMP}.dump.gz.enc"
ROLLING_DAYS="${BACKUP_ROLLING_DAYS:-7}"

mkdir -p "$BACKUP_DIR"

mark_status() {
  local ok="$1" detail="$2"
  # статус — в общий с worker/api том RUN_DIR (мониторинг §3.10)
  ${COMPOSE} exec -T worker python - "$ok" "$detail" <<'PY' || true
import os, sys
from pathlib import Path
from app.services import monitoring
monitoring.write_backup_status(
    Path(os.environ.get("RUN_DIR", "/var/lib/prosobesedovanie/run")),
    ok=(sys.argv[1] == "true"),
    detail=sys.argv[2],
)
PY
}

fail() {
  echo "backup failed: $1" >&2
  mark_status false "$1"
  exit 1
}

[ -n "${BACKUP_PASSPHRASE:-}" ] || fail "BACKUP_PASSPHRASE не задана"

# dump ролью backup через stdin (файл не попадает в контейнер db)
${COMPOSE} exec -T db pg_dump --username=backup --format=custom \
    --compress=9 prosobesedovanie 2>/tmp/backup_err.log \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
      -pass "env:BACKUP_PASSPHRASE" -out "$FILE" \
  || fail "pg_dump/openssl: $(tail -c 300 /tmp/backup_err.log 2>/dev/null)"

[ -s "$FILE" ] || fail "пустой файл копии"

# скользящие 7 дней (§3.9)
find "$BACKUP_DIR" -name 'prosobesedovanie-*.dump.gz.enc' \
     -mtime "+${ROLLING_DAYS}" -print -delete

mark_status true "$(basename "$FILE")"
echo "backup ok: $FILE"
