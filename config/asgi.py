"""
Hybrid ASGI Application (Django + FastAPI).
Запускается через Uvicorn: uvicorn config.asgi:application --reload
"""
import os
from django.core.asgi import get_asgi_application

# Инициализируем Django (должно быть до импорта FastAPI, чтобы settings загрузились)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django_app = get_asgi_application()

# Импортируем НАШ актуальный FastAPI роутер
from src.api.fastapi.routers import fastapi_router
from fastapi import FastAPI

# Создаем приложение FastAPI (только для документации и роутинга)
fastapi_app = FastAPI(
    title="AI Agents Platform - Streaming API",
    version="2.0.0",
    docs_url="/docs",       # Внутри FastAPI просто /docs
    redoc_url="/redoc",     # Внутри FastAPI просто /redoc
    root_path="/api/v2"
)

# Подключаем наши роутеры к FastAPI приложению
fastapi_app.include_router(fastapi_router)


# Главный роутер верхнего уровня
async def application(scope, receive, send):
    """
    Маршрутизатор решает, куда отправить запрос:
    - Если путь начинается с /api/v2/ — отдаем в FastAPI
    - Все остальное (вкл. /admin/, /api/v1/) — отдаем в Django
    """
    path = scope.get("path", "")

    if path.startswith("/api/v2/"):
        # Делегируем запрос в FastAPI
        await fastapi_app(scope, receive, send)
    else:
        # Делегируем запрос в Django
        await django_app(scope, receive, send)