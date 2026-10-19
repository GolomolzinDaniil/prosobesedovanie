"""Health-эндпоинты (§3.1).

GET /health/live  — 200, если API-процесс жив.
GET /health/ready — 200 только при доступности БД и корректной конфигурации;
иначе 503. Ответы не раскрывают ключи и адрес БД. /health/ready не зависит от
кратковременного отказа LLM (запасной сценарий), но конфигурационная ошибка
делает сервис неготовым (§3.10).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    reasons: list[str] = []
    config = getattr(request.app.state, "config", None)
    if config is None:
        reasons.append("config_not_loaded")
    db = getattr(request.app.state, "db", None)
    if db is None or not await db.ping():
        reasons.append("database_unavailable")

    if reasons:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reasons": reasons})

    # ai-статус раскрывается категорией без секретов и адресов
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "database": "up",
            "ai": "enabled" if config.ai_enabled else "disabled",
            "billing": "enabled" if config.settings.flags.billing_enabled else "disabled",
        },
    )
