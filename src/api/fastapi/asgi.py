"""
Standalone ASGI Application для FastAPI (Этап 5: Scaling).
Запускается отдельным процессом от Django.
Архитектурное решение: Мы принудительно вызываем django.setup(),
поскольку FastAPI процесс должен иметь доступ к Django ORM (чтение Run, запись Events),
но при этом полностью изолирован от синхронного цикла Django.
"""
import os
import django
from fastapi import FastAPI
import structlog

log = structlog.get_logger()

# 1. Инициализация Django ORM вне веб-контекста
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

# 2. Чистый FastAPI инстанс
app = FastAPI(
    title="AI Platform - Execution & Streaming API",
    version="2.0.0",
    # Выносим документацию под префикс /api/v2, чтобы не конфликтовать с Django
    docs_url="/api/v2/docs",
    openapi_url="/api/v2/openapi.json"
)

# 3. Подключаем наши роутеры (Execution, HITL, Time-Travel)
from src.api.fastapi.routers import fastapi_router
app.include_router(fastapi_router, prefix="/api/v2")

@app.on_event("startup")
async def startup_event():
    log.info("fastapi_standalone_started", mode="production_scaling")