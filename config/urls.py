"""
Root URL Configuration.
Тонкий слой: знает только об инфраструктуре (Admin, Health) и версионировании API.
Бизнес-логика доменов здесь НЕ импортируется.
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include




def health_check(request):
    """Базовая проверка живести сервиса (для Docker / Kubernetes / Load Balancers)."""
    return JsonResponse({"status": "ok", "service": "ai-agents-platform"})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_check, name='health-check'),
    path('api/v1/', include('src.api.drf.urls')),
]