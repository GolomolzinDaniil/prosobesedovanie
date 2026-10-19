-- Разделение ролей приложения, миграций и backup (§3.9).
-- Выполняется один раз при инициализации тома pgdata (docker-entrypoint-initdb.d).
-- Пароли задаются из окружения хоста; здесь — структура прав.

-- Роль миграций: DDL (создание/изменение схемы)
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'migrator') THEN
        CREATE ROLE migrator LOGIN PASSWORD 'CHANGE_ME_MIGRATOR';
    END IF;
END $$;

-- Роль backup: только чтение (pg_dump)
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup') THEN
        CREATE ROLE backup LOGIN PASSWORD 'CHANGE_ME_BACKUP';
    END IF;
END $$;

-- Приложение: DML без DDL (POSTGRES_USER=app владеет схемой при init;
-- в production право CREATE на схеме public у app отзывается)
GRANT USAGE ON SCHEMA public TO migrator, backup;
GRANT ALL ON SCHEMA public TO migrator;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
