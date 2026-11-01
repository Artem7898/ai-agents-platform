"""One FastAPI application factory for every supported entrypoint."""

from contextlib import asynccontextmanager

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.conf import settings
from django.db import close_old_connections
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.api.fastapi.routers import fastapi_router


class DatabaseConnections:
    """Match Django's request connection hygiene for ORM-backed FastAPI routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        async with ThreadSensitiveContext():
            await sync_to_async(close_old_connections, thread_sensitive=True)()
            try:
                await self.app(scope, receive, send)
            finally:
                await sync_to_async(close_old_connections, thread_sensitive=True)()


@asynccontextmanager
async def lifespan(_app):
    yield
    from src.agents.llm_service import close_llm_client

    await close_llm_client()


def create_application():
    app = FastAPI(
        title="AI Agents Platform - Execution API",
        version="2.0.0",
        docs_url="/api/v2/docs" if settings.DEBUG else None,
        redoc_url="/api/v2/redoc" if settings.DEBUG else None,
        openapi_url="/api/v2/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
    )
    app.include_router(fastapi_router, prefix="/api/v2")
    app.add_middleware(DatabaseConnections)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    return app
