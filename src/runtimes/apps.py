from django.apps import AppConfig


class RuntimesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "src.runtimes"
    label = "runtimes"
    verbose_name = "LLM runtimes"
