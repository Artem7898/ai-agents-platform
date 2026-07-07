# config/settings/dev_postgres.py
"""
Dev settings с реальным PostgreSQL.
Используйте, когда нужно тестировать Postgres-specific фичи (JSONB, ArrayField, pgvector).

Требует запущенный Postgres:
  docker-compose up -d postgres
"""
from .dev import *  # Наследуем всё из dev.py

# Переопределяем только БД на Postgres
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "ai_agents_dev",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5432",
        "CONN_MAX_AGE": 0,
    }
}

# Включаем OTel для тестирования трейсинга
NOVA_SETTINGS["TRACING_ENABLED"] = True
NOVA_SETTINGS["TRACING_SAMPLE_RATE"] = 1.0

print("\n🐳 Using PostgreSQL for development\n")