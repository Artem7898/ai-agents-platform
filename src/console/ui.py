"""Serve a built same-origin console; the page contains no embedded user data."""

from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def console(request, path=""):
    # API typos must remain JSON API errors, never a successful SPA response.
    if path == "api" or path.startswith("api/"):
        return HttpResponse('{"code":"not_found"}', status=404, content_type="application/json")
    index = Path(__file__).parent / "static" / "console" / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        message = "The workspace interface is unavailable. Please contact your administrator."
        if settings.DEBUG:
            message += " Build the console with: cd frontend && npm ci && npm run build."
        return HttpResponse(message, status=503, content_type="text/plain")
    response = HttpResponse(html)
    response["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    response["Referrer-Policy"] = "same-origin"
    return response
