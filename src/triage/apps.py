from importlib import import_module

from django.apps import AppConfig


class TriageConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "src.triage"
    label = "triage"
    verbose_name = "Ticket triage demo"

    def ready(self) -> None:
        import_module("src.triage.schemas")
        import_module("src.triage.tools")
