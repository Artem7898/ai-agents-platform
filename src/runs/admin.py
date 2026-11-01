"""Inspect durable execution state without bypassing workflow commands."""

from django.contrib import admin

from src.core.admin import NovaBaseAdmin

from .models import OutboxMessage, RunEvent, WorkflowRun


class ExecutionReadOnlyAdmin(NovaBaseAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WorkflowRun)
class WorkflowRunAdmin(ExecutionReadOnlyAdmin):
    list_display = ("short_id", "workflow", "status", "created_at")
    list_filter = ("status", "created_at")
    list_select_related = ("workflow",)
    search_fields = ("workflow__name",)


@admin.register(RunEvent)
class RunEventAdmin(ExecutionReadOnlyAdmin):
    list_display = ("run", "sequence", "event_type", "created_at")
    list_filter = ("event_type",)
    list_select_related = ("run",)


@admin.register(OutboxMessage)
class OutboxMessageAdmin(ExecutionReadOnlyAdmin):
    list_display = ("short_id", "run", "topic", "status", "attempts", "lease_expires_at")
    list_filter = ("topic", "status")
    list_select_related = ("run",)
