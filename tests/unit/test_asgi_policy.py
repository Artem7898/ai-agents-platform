"""Lifespan and security policy must cover FastAPI as well as Django."""

from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings
from httpx import ASGITransport, AsyncClient

from src.api.security import secure_application

pytestmark = pytest.mark.asyncio


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_trusted_hosts_are_checked_before_https_redirect():
    with override_settings(ALLOWED_HOSTS=["example.test"], SECURE_SSL_REDIRECT=True):
        app = secure_application(ok_app)
        async with AsyncClient(transport=ASGITransport(app=app)) as client:
            bad = await client.get("http://attacker.test/api/v2/runs/test")
            redirect = await client.get("http://example.test/api/v2/runs/test")
            secure = await client.get("https://example.test/api/v2/runs/test")
    assert bad.status_code == 400
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://example.test/api/v2/runs/test"
    assert secure.status_code == 200
    assert secure.headers["x-content-type-options"] == "nosniff"


async def test_forwarded_proto_is_not_trusted_by_default():
    with override_settings(
        ALLOWED_HOSTS=["example.test"],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=None,
    ):
        app = secure_application(ok_app)
        async with AsyncClient(transport=ASGITransport(app=app)) as client:
            response = await client.get(
                "http://example.test/api/v2/runs/test", headers={"X-Forwarded-Proto": "https"}
            )
    assert response.status_code == 307


async def test_explicit_trusted_proxy_matches_django_scheme_policy():
    with override_settings(
        ALLOWED_HOSTS=["example.test"],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ):
        app = secure_application(ok_app)
        async with AsyncClient(transport=ASGITransport(app=app)) as client:
            response = await client.get(
                "http://example.test/api/v2/runs/test", headers={"X-Forwarded-Proto": "https"}
            )
    assert response.status_code == 200


async def test_hybrid_lifespan_reaches_fastapi_and_closes_llm_client():
    from config.asgi import application

    received = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent = []

    async def receive():
        return next(received)

    async def send(message):
        sent.append(message["type"])

    with patch("src.agents.llm_service.close_llm_client", new_callable=AsyncMock) as close:
        await application({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    close.assert_awaited_once()
