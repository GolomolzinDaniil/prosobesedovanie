# Один контейнерный образ запускается в двух процессах (§2.1):
#   api    -> python -m app.api.main      (webhook + health)
#   worker -> python -m app.workers.main  (команды, AI-задания, исходящие)
#   migrate-> python -m app.storage.migrate
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/prosobesedovanie

# Зависимости фиксируются lock-файлом версий (requirements.txt)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY contracts ./contracts
COPY migrations ./migrations
COPY config ./config
COPY prompts ./prompts
COPY scripts ./scripts

# Непривилегированный пользователь (§3.9)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/prosobesedovanie/media /var/lib/prosobesedovanie/run \
    && chown -R appuser:appuser /var/lib/prosobesedovanie /srv/prosobesedovanie \
    && chmod 700 /var/lib/prosobesedovanie/media /var/lib/prosobesedovanie/run
USER appuser

# Временная директория аудио: 0700 (§3.5); RUN_DIR — heartbeat/статус backup
ENV MEDIA_DIR=/var/lib/prosobesedovanie/media \
    RUN_DIR=/var/lib/prosobesedovanie/run \
    API_HOST=0.0.0.0 \
    API_PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.api.main"]
