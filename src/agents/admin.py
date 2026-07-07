from django.contrib import admin
from src.core.admin import NovaBaseAdmin, get_status_badge
from .models import Agent, AgentStatus


@admin.register(Agent)
class AgentAdmin(NovaBaseAdmin):
    list_display = ('short_id', 'name', 'model_name', 'get_status_badge', 'temperature', 'created_at')
    list_display_links = ('name', 'short_id')
    list_filter = ('status', 'model_name', 'created_at')
    search_fields = ('name', 'system_prompt')

    # Быстрое редактирование прямо в списке
    list_editable = ('temperature',)

    readonly_fields = ('id', 'created_at', 'updated_at', 'short_id')

    fieldsets = (
        ("Основная информация", {
            'fields': ('id', 'name', 'status')
        }),
        ("LLM Конфигурация", {
            'fields': ('model_name', 'temperature', 'system_prompt'),
            'description': 'Настройки модели для генерации ответов.'
        }),
        ("Инструменты (Tools)", {
            'fields': ('tool_ids',),
            'description': 'Массив UUID инструментов, доступных агенту.'
        }),
        ("Системные метки", {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)  # Свернуто по умолчанию
        }),
    )

    def get_status_badge(self, obj):
        colors = {
            AgentStatus.DRAFT: "#6b7280",  # Серый
            AgentStatus.ACTIVE: "#10b981",  # Зеленый
            AgentStatus.ARCHIVED: "#f59e0b",  # Желтый
        }
        return get_status_badge(obj.status, colors)

    get_status_badge.short_description = "Статус"