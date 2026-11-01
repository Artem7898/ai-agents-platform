from importlib import import_module

from django.apps import AppConfig


class DomainAgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "src.agents"
    label = "domain_agents"
    verbose_name = "Domain Agents"

    def ready(self) -> None:
        import_module("src.agents.tools")
