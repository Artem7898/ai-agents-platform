from django.contrib import admin
from src.core.admin import NovaBaseAdmin
from .models import Workflow

@admin.register(Workflow)
class WorkflowAdmin(NovaBaseAdmin):
    list_display = ('short_id', 'name', 'version', 'get_nodes_count', 'get_edges_count', 'updated_at')
    list_display_links = ('name', 'short_id')
    list_filter = ('version',)
    search_fields = ['name']
    readonly_fields = ('id', 'created_at', 'updated_at', 'short_id', 'get_nodes_count',)

    fieldsets = (
        ("Метаданные", {
            'fields': ('id', 'name', 'version')
        }),
        ("DAG Структура (Graph)", {
            'fields': ('nodes', 'edges'),
            'description': 'JSON-представление направленного ациклического графа (DAG).'
        }),
        ("Системные метки", {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    # Выводим количество узлов/ребер в списке для быстрого обзора
    def get_nodes_count(self, obj):
        return len(obj.nodes)
    get_nodes_count.short_description = "Узлов"

    def get_edges_count(self, obj):
        return len(obj.edges)
    get_edges_count.short_description = "Связей"