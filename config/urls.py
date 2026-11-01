"""Administrative, readiness and definition API routes."""

from asgiref.sync import sync_to_async
from django.contrib import admin
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.urls import include, path


def health_check(request):
    return JsonResponse({"status": "ok", "service": "ai-agents-platform"})


def database_ready():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone() == (1,)


async def readiness_check(request):
    try:
        ready = await sync_to_async(database_ready, thread_sensitive=True)()
    except DatabaseError:
        ready = False
    return JsonResponse(
        {"status": "ready" if ready else "unavailable", "database": ready},
        status=200 if ready else 503,
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health-check"),
    path("ready/", readiness_check, name="readiness-check"),
    path("api/v1/", include("src.api.drf.urls")),
]
