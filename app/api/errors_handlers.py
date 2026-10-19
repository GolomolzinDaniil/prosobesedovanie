"""HTTP-слой ошибок и request_id (§3.1–3.2).

Отказ HTTP до создания доверенного Context использует тот же ErrorResponse
с новым request_id. Ответы не раскрывают ключи и адрес БД. Успешный webhook
имеет короткий собственный ответ протокола Telegram ({"ok": true}).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.domain.errors import DomainError
from contracts.models import ErrorCode

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """request_id на каждый запрос; отдаётся заголовком X-Request-Id."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4()
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = str(request_id)
        return response


def request_id_of(request: Request) -> uuid.UUID:
    return getattr(request.state, "request_id", None) or uuid.uuid4()


def error_response(
    code: ErrorCode,
    *,
    http_status: int,
    request_id: uuid.UUID,
    message: str | None = None,
    retryable: bool | None = None,
    field: str | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    from app.domain.errors import ERROR_REGISTRY  # локальный импорт против цикла

    default_message, default_retryable = ERROR_REGISTRY[code]
    body = {
        "ok": False,
        "error": {
            "code": code.value,
            "message": message or default_message,
            "retryable": default_retryable if retryable is None else retryable,
            "field": field,
            "retry_after_seconds": retry_after_seconds,
        },
        "request_id": str(request_id),
    }
    return JSONResponse(status_code=http_status, content=body)


def domain_error_response(
    exc: DomainError, request_id: uuid.UUID, http_status: int
) -> JSONResponse:
    return error_response(
        exc.code,
        http_status=http_status,
        request_id=request_id,
        message=exc.message,
        retryable=exc.retryable,
        field=exc.field,
        retry_after_seconds=exc.retry_after_seconds,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.INVALID_INPUT,
            status.HTTP_413_CONTENT_TOO_LARGE: ErrorCode.PAYLOAD_TOO_LARGE,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            status.HTTP_503_SERVICE_UNAVAILABLE: ErrorCode.SERVICE_UNAVAILABLE,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        detail = exc.detail if isinstance(exc.detail, str) else None
        return error_response(
            code,
            http_status=exc.status_code,
            request_id=request_id_of(request),
            message=detail,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Публичные точки не принимают доменные тела напрямую; ошибка формы —
        # некорректный запрос. Детали pydantic наружу не раскрываются (секреты
        # и трассировка в сообщение не попадают, §3.2).
        return error_response(
            ErrorCode.INVALID_INPUT,
            http_status=status.HTTP_400_BAD_REQUEST,
            request_id=request_id_of(request),
            message="Некорректный запрос.",
        )

    @app.exception_handler(DomainError)
    async def _domain_exception(request: Request, exc: DomainError) -> JSONResponse:
        return domain_error_response(exc, request_id_of(request), status.HTTP_400_BAD_REQUEST)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        rid = request_id_of(request)
        # В лог — только request_id и тип ошибки, без содержимого (§3.9)
        logger.exception("unhandled error", extra={"request_id": str(rid)})
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            request_id=rid,
        )
