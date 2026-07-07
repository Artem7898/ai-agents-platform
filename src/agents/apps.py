from django.apps import AppConfig

class DomainAgentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'src.agents'
    label = 'domain_agents'     # Уникальный лейбл (чтобы не конфликтовать с пакетом nova)
    verbose_name = 'Domain Agents'