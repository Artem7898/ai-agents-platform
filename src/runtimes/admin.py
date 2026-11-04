from django.contrib import admin

from .models import LLMRuntime


@admin.register(LLMRuntime)
class LLMRuntimeAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "profile", "enabled", "revision", "updated_at")
    list_filter = ("profile", "enabled")
    search_fields = ("name", "base_url")
    readonly_fields = ("id", "revision", "created_at", "updated_at")
