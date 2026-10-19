# Чеклист развёртывания и запуска (§3.10, T23; приёмка AT17)

Стартовое размещение: один Linux-сервер 2 vCPU / 4 GiB RAM / SSD 20 GiB;
API, worker и PostgreSQL в контейнерах; reverse proxy с TLS перед API.
Это исходная ёмкость для нагрузочного теста, не доказанная производительность.

## 0. Предварительные условия (условия запуска)

- [ ] Отдельные боты и ключи для dev / staging / production (BotFather).
- [ ] `PUBLIC_BASE_URL` — HTTPS (reverse proxy с действующим сертификатом).
- [ ] Заполнен `config/provider-profile.json`: провайдеры LLM/STT/TTS,
      тарифы, `data_retention_terms`, smoke-поля проверок провайдера
      (JSON-запрос LLM, русский STT, OGG/Opus TTS) с датами (§3.6, §5.3).
- [ ] `settings.costs.daily_ai_budget_usd` и тарифы заполнены (без них
      startup-валидация предупреждает, внешние вызовы блокируются §1.4).
- [ ] Условия хранения у AI-провайдера выбраны и описаны пользователю
      (§3.9); если провайдер не даёт требуемых условий — сменить провайдера
      либо не выпускать соответствующий режим.
- [ ] Реальные получатель и канал уведомлений оператора назначены (§3.10:
      сигналы — не автоматическая отправка людям).
- [ ] `.env` заполнен (см. `config/.env.example`): BOT_TOKEN, WEBHOOK_SECRET,
      WEBHOOK_ROUTE_TOKEN, DATABASE_URL, MIGRATIONS_DATABASE_URL,
      POSTGRES_PASSWORD, MIGRATOR_PASSWORD, *_API_KEY, ENV_NAME,
      PUBLIC_BASE_URL, SUPPORT_CONTACT. Секреты — только из защищённого
      хранилища, не из репозитория.
- [ ] BACKUP_PASSPHRASE передана оператору через защищённый канал.
- [ ] Роли БД разделены: app (DML), migrator (DDL), backup (SELECT) —
      `deploy/init-db-roles.sql`, пароли заменены с CHANGE_ME.

## 1. Стартовая последовательность (§3.10 «При запуске»)

1. [ ] Проверить обязательные переменные, профиль и settings:
       `docker compose run --rm migrate python -c "from app.config import load_config; load_config(); print('config ok')"`.
2. [ ] Выполнить миграции: `docker compose run --rm migrate`
       (роль migrator через MIGRATIONS_DATABASE_URL).
3. [ ] Запустить worker: `docker compose --profile workers up -d worker`
       (при запуске выполняется startup cleanup медиа и сверка tombstones).
4. [ ] Запустить API: `docker compose up -d api`.
5. [ ] Установить webhook с секретом и разрешёнными видами update:
       `docker compose exec api python scripts/set_webhook.py`
       (allowed_updates: message, callback_query; +pre_checkout_query при
       billing_enabled=true).
6. [ ] Проверить health и сигналы: `python scripts/smoke.py --webhook`
       (live/ready 200, heartbeat свежий, зависших lease нет).
7. [ ] Проверить первый вопрос и оба формата тестовым аккаунтом:
       - [ ] /start → согласие → роль → уровень → формат «Текст» → первый
             вопрос доставлен ≤ 5 с (цель §1.3);
       - [ ] полный текстовый прогон: 6 вопросов (до 2 уточнений) → отчёт
             1–4 части, цитаты из ответов;
       - [ ] формат «Голос»: voice ≤ 120 с / 10 MiB → расшифровка → экран
             подтверждения → «Исправить» и «Записать заново» работают →
             озвучка вопроса приходит после текста;
       - [ ] /report повторно возвращает сохранённый отчёт без нового
             LLM-вызова; /status, /balance, /help, /delete отрабатывают.
8. [ ] Зафиксировать версии: `python scripts/make_version_manifest.py
       --profile config/provider-profile.json` → planning/version-manifest.json
       (зависимости, образ, миграции, промпты, профиль — §3.10).
9. [ ] Включить ежедневный backup (cron): 
       `BACKUP_PASSPHRASE=... /path/to/scripts/backup.sh /secure/backups`
       (шифрование AES-256, скользящие 7 дней, статус в RUN_DIR).
10. [ ] Настроить ротацию технических логов: контейнеры — logging max-size/
       max-file в docker-compose.yml (~14 дней, §3.9); серверные журналы —
       logrotate хостинга. Логи без текстов ответов.
11. [ ] Мониторинг: cron `python scripts/check_signals.py --json` (или
       внешний мониторинг по логам `metrics`/`operator_signal`); канал
       доставки сигналов оператору подключён.

## 2. Восстановление (AT17: restore без дублирования прав и отправок)

1. [ ] Остановить трафик: `scripts/restore.sh` делает это первым шагом.
2. [ ] `BACKUP_PASSPHRASE=... scripts/restore.sh <файл-копии>`:
       расшифровка → pg_restore → **повторное применение удалений по
       tombstones ДО приёма трафика** → запуск worker/api → smoke.
3. [ ] Проверить отсутствие дублей: права (credits) не начислены повторно
       (заряды защищены charge_id/UNIQUE), outbox не отправляет доставленное
       заново (unique_key), inbox-дубли подтверждены без повторной команды.
4. [ ] Записать фактические RPO/RTO в журнал ниже (проектные цели: RPO ≤ 24 ч,
       RTO ≤ 2 ч — подтверждаются восстановлением staging, §3.10).
5. [ ] Перед включением оплаты: уменьшить RPO и проверить сверку Stars
       (журнал транзакций Telegram — источник платёжного восстановления).

## 3. Окружения

- [ ] ENV_NAME=staging для staging; production не принимает тестовые
      обращения без обозначения (отдельный бот/ключи).
- [ ] dev/staging/prod используют разные БД и разные RUN_DIR/MEDIA_DIR.
- [ ] Сетевой доступ к PostgreSQL — только внутренняя сеть compose
      (порт наружу не публикуется, §3.9).

## Журнал развёртываний и восстановлений

| Дата | Окружение | Версии (manifest) | Операция | RPO/RTO факт | Ответственный | Результат |
|---|---|---|---|---|---|---|
| (заполняется при фактических операциях) | | | | | | |
