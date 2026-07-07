from django.apps import AppConfig

class RunsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "src.runs"           # Путь к модулю
    label = "runs"              # Уникальный лейбл для runs
    verbose_name = "Runs"