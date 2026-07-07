from django.contrib import admin
from django.utils.html import format_html_join, format_html
from src.core.admin import NovaBaseAdmin, get_status_badge
from .models import WorkflowRun, RunStatus

@admin.register(WorkflowRun)
class WorkflowRunAdmin(NovaBaseAdmin):
    list_display = ('short_id', 'get_workflow_link', 'get_status_badge', 'created_at')
    list_display_links = ('short_id',)
    list_filter = ('status', 'created_at')
    readonly_fields = ('id', 'created_at', 'updated_at', 'short_id', 'get_events_log')

    # Современный поиск по FK (вместо выпадающего списка)
    autocomplete_fields = ['workflow']

    fieldsets = (
        ("Контекст выполнения", {
            'fields': ('id', 'workflow', 'status')
        }),
        ("Входные и выходные данные", {
            'fields': ('input_data', 'output_data'),
            'classes': ('wide',) # Растягиваем поля на всю ширину
        }),
        ("Event Sourcing Лог", {
            'fields': ('get_events_log',),
            'classes': ('collapse',),
            'description': 'История всех событий выполнения (Time-travel). Только чтение.'
        }),
        ("Системные метки", {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_status_badge(self, obj):
        colors = {
            RunStatus.PENDING: "#6b7280",
            RunStatus.RUNNING: "#3b82f6",   # Синий
            RunStatus.WAITING_HUMAN: "#f59e0b",
            RunStatus.COMPLETED: "#10b981",
            RunStatus.FAILED: "#ef4444",    # Красный
        }
        return get_status_badge(obj.status, colors)
    get_status_badge.short_description = "Статус"

    def get_workflow_link(self, obj):
        # Делаем кликабельную ссылку на воркфлоу прямо из списка запусков
        from django.urls import reverse
        url = reverse('admin:workflows_workflow_change', args=[obj.workflow.id])
        return format_html('<a href="{}">{} (v{})</a>', url, obj.workflow.name, obj.workflow.version)
    get_workflow_link.short_description = "Workflow"

    def get_events_log(self, obj):
        import json
        if not obj.events:
            return "Нет событий"
        return format_html("<pre style='font-size: 11px; background: #f8f9fa; padding: 10px; border-radius: 4px;'>{}</pre>",
                          json.dumps(obj.events, indent=2, ensure_ascii=False))
    get_events_log.short_description = "Лог событий"