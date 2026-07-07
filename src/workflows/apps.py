from django.apps import AppConfig

class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "src.workflows"      # Путь к модулю
    label = "workflows"         # Уникальный лейбл для workflows
    verbose_name = "Workflows"