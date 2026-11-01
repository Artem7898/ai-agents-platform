"""Security policy shared by the hybrid and standalone ASGI entrypoints."""

from django.conf import settings
from starlette.datastructures import MutableHeaders
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "same-origin")
                if scope.get("scheme") == "https" and settings.SECURE_HSTS_SECONDS:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={settings.SECURE_HSTS_SECONDS}",
                    )
            await send(message)

        await self.app(scope, receive, secured_send)


class TrustedProxyScheme:
    """Honor the same explicit trusted-proxy opt-in as Django."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        configured = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
        if configured and scope["type"] in {"http", "websocket"}:
            header, secure_value = configured
            name = header.removeprefix("HTTP_").replace("_", "-").lower().encode()
            values = dict(scope.get("headers", []))
            if name in values:
                secure = values[name].decode("latin-1").split(",")[0].strip() == secure_value
                scope = dict(scope)
                scope["scheme"] = (
                    ("wss" if secure else "ws")
                    if scope["type"] == "websocket"
                    else ("https" if secure else "http")
                )
        await self.app(scope, receive, send)


def secure_application(app):
    app = SecurityHeaders(app)
    if getattr(settings, "SECURE_SSL_REDIRECT", False):
        app = HTTPSRedirectMiddleware(app)
    allowed = []
    for host in settings.ALLOWED_HOSTS:
        if host.startswith("."):
            allowed.extend([host[1:], "*" + host])
        else:
            allowed.append(host)
    app = TrustedHostMiddleware(app, allowed_hosts=allowed, www_redirect=False)
    return TrustedProxyScheme(app)
