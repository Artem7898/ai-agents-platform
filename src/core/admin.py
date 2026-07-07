from django.contrib import admin
from django.utils.html import format_html

class NovaBaseAdmin(admin.ModelAdmin):
    """
    Базовый класс для админки платформы.
    Добавляет современный UI (бейджи) и общие настройки.
    """
    # Показываем короткие UUID вместо длинных
    def short_id(self, obj):
        return str(obj.id)[:8]
    short_id.short_description = "ID"

    # Отключаем стандартный выбор страницы (всегда показываем все)
    list_per_page = 50

    # Читаемость JSON в формах (Django < 5.1 рендерит JSON как текстареа)
    formfield_overrides = {
        # Если у тебя Django 5.1+, это не понадобится, там уже красивый редактор
    }


def get_status_badge(status_value: str, color_map: dict) -> str:
    """Утилита для рендеринга цветных бейджей статусов."""
    color = color_map.get(status_value, "#6b7280") # Серый по умолчанию
    return format_html(
        '<span style="background-color: {}; color: white; padding: 3px 8px; '
        'border-radius: 4px; font-size: 12px; font-weight: 500;">{}</span>',
        color, status_value
    )