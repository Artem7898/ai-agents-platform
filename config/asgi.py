"""Hybrid ASGI with one lifespan and explicit security for both API stacks."""

import os

from django.core.asgi import get_asgi_application

from src.api.security import secure_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")


def build_apps():
    django_app = get_asgi_application()
    # Model-backed routers must be imported after Django's app registry is ready.
    from src.api.fastapi.application import create_application

    return django_app, create_application()


django_app, fastapi_app = build_apps()


async def dispatch(scope, receive, send):
    if scope["type"] == "lifespan":
        await fastapi_app(scope, receive, send)
    elif scope.get("path", "").startswith("/api/v2/"):
        await fastapi_app(scope, receive, send)
    elif scope["type"] == "http":
        await django_app(scope, receive, send)
    elif scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})


application = secure_application(dispatch)
