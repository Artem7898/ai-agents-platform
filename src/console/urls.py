"""Dedicated session routes; the public token API registry is unchanged."""

from django.conf import settings
from django.urls import path
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.routers import DefaultRouter

from src.api.drf.urls import router as token_router

from . import auth
from .api import (
    ConsolePagination,
    ConsoleRunViewSet,
    ConsoleSessionAuthentication,
    DashboardView,
    ExecuteView,
    OperationsView,
    ToolsView,
)

router = DefaultRouter()
_PERSONAL_PATHS = {
    "agents": ("owner", None),
    "workflows": ("owner", None),
    "runtimes": ("owner", None),
    "llm-calls": ("owner", None),
    "llm-quotas": ("owner", None),
    "llm-quota-windows": ("quota__owner", None),
    "billing-subscriptions": ("account__owner", "account__mode"),
    "billing-periods": ("subscription__account__owner", "subscription__account__mode"),
    "billing-invoices": (
        "period__subscription__account__owner",
        "period__subscription__account__mode",
    ),
    "billing-payments": (
        "invoice__period__subscription__account__owner",
        "invoice__period__subscription__account__mode",
    ),
}


def _personal_queryset(base, prefix):
    def get_queryset(self):
        queryset = base.get_queryset(self)
        if self.request.query_params.get("scope") == "mine":
            owner_path, mode_path = _PERSONAL_PATHS[prefix]
            filters = {owner_path: self.request.user}
            if mode_path:
                filters[mode_path] = settings.BILLING_MODE
            queryset = queryset.filter(**filters)
        return queryset

    return get_queryset


for prefix, viewset, basename in token_router.registry:
    console_viewset = (
        ConsoleRunViewSet
        if prefix == "runs"
        else type(
            "Console" + viewset.__name__,
            (viewset,),
            {
                "authentication_classes": (ConsoleSessionAuthentication,),
                "pagination_class": ConsolePagination,
                "__module__": __name__,
                **(
                    {"get_queryset": _personal_queryset(viewset, prefix)}
                    if prefix in _PERSONAL_PATHS
                    else {}
                ),
            },
        )
    )
    console_viewset = method_decorator(never_cache, name="dispatch")(console_viewset)
    router.register(prefix, console_viewset, basename="console-" + basename)

urlpatterns = [
    path("session/", auth.session, name="console-session"),
    path("login/", auth.sign_in, name="console-login"),
    path("logout/", auth.sign_out, name="console-logout"),
    path("password/", auth.password, name="console-password"),
    path("profile/", auth.profile, name="console-profile"),
    path("execute/", ExecuteView.as_view(), name="console-execute"),
    path("dashboard/", DashboardView.as_view(), name="console-dashboard"),
    path("tools/", ToolsView.as_view(), name="console-tools"),
    path("operations/", OperationsView.as_view(), name="console-operations"),
] + router.urls
