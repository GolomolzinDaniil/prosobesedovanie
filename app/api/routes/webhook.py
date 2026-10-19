"""POST /telegram/webhook/{route_token} — единственная внешняя бизнес-точка (§3.1).

Протокол:
- route_token случайный и не содержит токен бота; неверный -> 404 (точка не
  раскрывается);
- X-Telegram-Bot-Api-Secret-Token сравнивается с секретом; неверный -> 403;
- только application/json, не более 1 MiB: неверный тип -> 415, размер -> 413,
  неверная структура -> 400;
- поддерживаемый вид сохраняется в inbox и получает 200 {"ok": true}; повтор
  update_id тоже получает 200;
- неподдерживаемый вид признаётся обработанным без вызова AI (inbox skipped);
- PostgreSQL недоступен -> 503, чтобы Telegram повторил доставку.
"""

from __future__ import annotations

import hmac
import json
import logging

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.errors_handlers import error_response, request_id_of
from app.storage import inbox_repo
from app.telegram.updates import InvalidUpdateError, classify_update
from contracts.models import ErrorCode

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_BODY_FALLBACK = 1024 * 1024  # 1 MiB (§3.1), уточняется из settings


@router.post("/telegram/webhook/{route_token}")
async def telegram_webhook(request: Request, route_token: str) -> JSONResponse:
    request_id = request_id_of(request)
    config = request.app.state.config
    max_body = config.settings.limits.webhook_body_max_bytes or _MAX_BODY_FALLBACK

    # 1. Происхождение: route_token в URL и секрет-заголовок (§3.1)
    if not hmac.compare_digest(route_token, config.env.webhook_route_token):
        return error_response(
            ErrorCode.NOT_FOUND,
            http_status=404,
            request_id=request_id,
            message="Не найдено.",
        )
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header is None or not hmac.compare_digest(
        secret_header, config.env.webhook_secret
    ):
        return error_response(
            ErrorCode.FORBIDDEN,
            http_status=403,
            request_id=request_id,
            message="Доступ запрещён.",
        )

    # 2. Только JSON (неверный тип -> 415)
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return error_response(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            http_status=415,
            request_id=request_id,
        )

    # 3. Размер: Content-Length и фактическое чтение с жёстким пределом (-> 413)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_body:
                return error_response(
                    ErrorCode.PAYLOAD_TOO_LARGE, http_status=413, request_id=request_id
                )
        except ValueError:
            pass  # недостоверный заголовок — проверяем фактический размер ниже
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body:
            return error_response(
                ErrorCode.PAYLOAD_TOO_LARGE, http_status=413, request_id=request_id
            )

    # 4. Структура: неверный JSON или Update -> 400
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(
            ErrorCode.INVALID_UPDATE, http_status=400, request_id=request_id
        )
    try:
        kind, supported = classify_update(
            payload, billing_enabled=config.settings.flags.billing_enabled
        )
        update_id = int(payload["update_id"])
    except InvalidUpdateError as exc:
        logger.info(
            "webhook rejected update", extra={"request_id": str(request_id), "reason": exc.reason}
        )
        return error_response(
            ErrorCode.INVALID_UPDATE,
            http_status=400,
            request_id=request_id,
            message=f"Тело запроса не является корректным Telegram Update: {exc.reason}",
        )

    # 5. Сохранение в inbox с дедупликацией; БД недоступна -> 503
    db = getattr(request.app.state, "db", None)
    if db is None:
        return error_response(
            ErrorCode.SERVICE_UNAVAILABLE,
            http_status=503,
            request_id=request_id,
            retry_after_seconds=5,
        )
    try:
        async with db.pool.acquire() as conn:
            inserted = await inbox_repo.insert_update(
                conn,
                update_id=update_id,
                kind=kind,
                payload=payload,
                initial_status="pending" if supported else "skipped",
            )
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        logger.warning(
            "webhook db failure",
            extra={"request_id": str(request_id), "error": type(exc).__name__},
        )
        return error_response(
            ErrorCode.SERVICE_UNAVAILABLE,
            http_status=503,
            request_id=request_id,
            retry_after_seconds=5,
        )

    # 6. Короткий ответ протокола Telegram: 200 при новом и при повторном update
    logger.info(
        "webhook stored",
        extra={
            "request_id": str(request_id),
            "kind": kind,
            "stored": inserted,
            "supported": supported,
        },
    )
    return JSONResponse(status_code=200, content={"ok": True})
