from django.contrib import admin

from src.usage.admin import ReadOnlyLedgerAdmin

from .models import (
    BillingAccount,
    BillingPeriod,
    Invoice,
    PaymentReceipt,
    PlanVersion,
    Subscription,
)


@admin.register(PlanVersion)
class PlanVersionAdmin(admin.ModelAdmin):
    list_display = ("code", "version", "name", "price_minor", "currency", "enabled")

    def get_readonly_fields(self, request, obj=None):
        return ("id", "created_at") + (PlanVersion.frozen_fields if obj else ())

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ("owner", "mode", "blocked", "created_at")
    readonly_fields = ("id", "owner", "mode", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyLedgerAdmin):
    list_display = ("id", "account", "plan", "status", "cancel_at_period_end")


@admin.register(BillingPeriod)
class BillingPeriodAdmin(ReadOnlyLedgerAdmin):
    list_display = ("id", "subscription", "starts_at", "ends_at", "paid_at", "closed_at")


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyLedgerAdmin):
    list_display = (
        "id",
        "kind",
        "status",
        "amount_minor",
        "currency",
        "settlement_kind",
        "issued_at",
    )


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(ReadOnlyLedgerAdmin):
    list_display = ("id", "invoice", "provider", "mode", "amount_minor", "currency", "recorded_by")
