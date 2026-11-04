from django.contrib import admin

from .models import LLMInvocation, LLMRate, OwnerQuota, QuotaWindow


@admin.register(OwnerQuota)
class OwnerQuotaAdmin(admin.ModelAdmin):
    list_display = ("owner", "token_limit", "request_limit", "period_seconds", "enabled")
    readonly_fields = ("created_at",)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("owner", "period_seconds") if obj else ())

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyLedgerAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LLMInvocation)
class LLMInvocationAdmin(ReadOnlyLedgerAdmin):
    list_display = (
        "id",
        "owner",
        "model_name",
        "status",
        "usage_status",
        "total_tokens",
        "started_at",
    )
    list_filter = ("status", "usage_status", "provider")


@admin.register(QuotaWindow)
class QuotaWindowAdmin(ReadOnlyLedgerAdmin):
    list_display = ("quota", "starts_at", "ends_at", "used_tokens", "reserved_tokens", "used_calls")


@admin.register(LLMRate)
class LLMRateAdmin(admin.ModelAdmin):
    list_display = ("model_name", "runtime", "version", "currency", "effective_from")
    readonly_fields = ("id", "created_at")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
