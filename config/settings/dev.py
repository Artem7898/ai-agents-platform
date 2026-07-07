"""
Development-specific Django settings for AI Agents Platform.

Этот файл наследует всё из base.py и переопределяет только то,
что критично для локальной разработки:
- Упрощённая БД (SQLite по умолчанию, Postgres через env)
- In-memory кэш (не требует Redis)
- Отключён OTel-экспорт (чтобы не спамить в несуществующий Tempo)
- Максимальная детализация логов
- Разрешён CORS для любого frontend-порта

WHY THIS MATTERS:
- Новый разработчик может склонировать репо, сделать `pip install -e .[dev]`
  и сразу запустить `python manage.py runserver` БЕЗ поднятия Docker-контейнеров.
- При этом вся бизнес-логика (Django Nova, Pydantic, async ORM) работает идентично проде.
"""

from .base import *  # noqa: F401, F403  (наследуем всё из base.py)
from .base import env, BASE_DIR, NOVA_SETTINGS, REST_FRAMEWORK, CACHES, DATABASES

# ==============================================================================
# 1. CORE DEBUG SETTINGS
# ==============================================================================

DEBUG = True
SECRET_KEY = "django-insecure-dev-key-do-not-use-in-prod-!@#$%^&*()"
ALLOWED_HOSTS = ["*"]  # В проде это переопределяется в prod.py

# Для django-debug-toolbar (опционально, если установлен)
INTERNAL_IPS = ["127.0.0.1", "localhost"]

# ==============================================================================
# 2. DATABASE (Dev-Friendly)
# ==============================================================================
# Стратегия: если в .env указан DATABASE_URL — используем его (Postgres).
# Если нет — fallback на SQLite, чтобы не требовать Docker для старта.

if not env.DATABASE_URL or env.DATABASE_URL == "postgres://postgres:postgres@localhost:5432/ai_agents":
    # Fallback на SQLite (не требует Postgres)
    # ВАЖНО: SQLite НЕ поддерживает JSONB/ArrayField из Postgres.
    # Для полноценного тестирования Django Nova с JSONB-полями
    # нужно поднять Postgres и указать DATABASE_URL в .env.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # Отключаем persistent connections для SQLite
            "CONN_MAX_AGE": 0,
            "TEST": {
                "NAME": BASE_DIR / "test_db.sqlite3",
            },
        }
    }
else:
    # Используем Postgres из .env (как в base.py), но без persistent connections
    # для локальной разработки (чтобы не держать соединения между перезапусками)
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = False

# ==============================================================================
# 3. CACHING (In-Memory, без Redis)
# ==============================================================================
# В локации Redis может быть не поднят. Используем LocMemCache —
# он работает в рамках одного процесса, чего достаточно для dev.
# ВАЖНО: кэш НЕ переживает перезапуск сервера, но для dev это ок.

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ai-agents-dev-cache",
        "TIMEOUT": 60,  # Короткий TTL для dev
        "OPTIONS": {
            "MAX_ENTRIES": 1000,
        },
    }
}

# ==============================================================================
# 4. DJANGO NOVA (Relaxed for Dev)
# ==============================================================================
# Отключаем OTel-экспорт в локалке, чтобы не было ошибок при отсутствии
# Tempo/Jaeger. Трейсинг всё ещё работает внутри процесса (для structlog).

NOVA_SETTINGS["TRACING_ENABLED"] = False
NOVA_SETTINGS["TRACING_SAMPLE_RATE"] = 0.0
NOVA_SETTINGS["INCLUDE_SQL_QUERIES"] = True  # Но SQL-запросы логируем

# Pydantic strict mode оставляем ВКЛЮЧЁННЫМ — это помогает ловить баги рано.
# NOVA_SETTINGS["PYDANTIC_STRICT_MODE"] = True  # уже в base.py

# ==============================================================================
# 5. LOGGING (Verbose for Dev)
# ==============================================================================
# В локации хотим видеть ВСЁ: DEBUG-уровень, цветной вывод, SQL-запросы.

LOG_LEVEL = "DEBUG"

# Переопределяем structlog на более подробный вывод
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(colors=True, sort_keys=False),
    ],
    # ИСПРАВЛЕНО: используем logging.DEBUG вместо structlog.DEBUG
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    context_class=dict,
    logger_factory=structlog.WriteLoggerFactory(),
    cache_logger_on_first_use=False,
)

# Показываем SQL-запросы в консоли (полезно для отладки async ORM)
LOGGING["loggers"]["django.db.backends"] = {
    "handlers": ["console"],
    "level": "DEBUG",
    "propagate": False,
}

# ==============================================================================
# 6. DJANGO REST FRAMEWORK (Dev-Friendly)
# ==============================================================================
# Отключаем throttling в локации, чтобы не получать 429 при тестах.
# Включаем BrowsableAPIRenderer для удобной работы через браузер.

REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",  # UI для ручного тестирования
]

REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # Отключаем throttling
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {}

# Упрощаем аутентификацию для dev (Session + Basic для тестов)
REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] = [
    "rest_framework.authentication.SessionAuthentication",
    "rest_framework.authentication.BasicAuthentication",
]

# В dev не требуем аутентификацию по умолчанию (для быстрого прототипирования)
# В prod это переопределяется на IsAuthenticated
REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] = [
    "rest_framework.permissions.AllowAny",
]

# ==============================================================================
# 7. CORS (Permissive for Dev)
# ==============================================================================
# Разрешаем запросы с любого порта локального фронтенда
# (localhost:3000, 5173, 8080 и т.д.)

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# ==============================================================================
# 8. EMAIL (Console Backend)
# ==============================================================================
# Все письма "отправляются" в консоль — удобно для тестирования
# workflow с human-in-the-loop уведомлениями.

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ==============================================================================
# 9. STATIC & MEDIA FILES (Dev)
# ==============================================================================
# В dev используем Django's built-in static file server.
# В prod это будет Nginx/S3.

STATIC_URL = "/static/"
MEDIA_URL = "/media/"

# ==============================================================================
# 10. OPTIONAL: DEBUG TOOLBAR (если установлен)
# ==============================================================================
# Раскомментируйте, если установили django-debug-toolbar:
# pip install django-debug-toolbar

# try:
#     import debug_toolbar  # noqa
#     INSTALLED_APPS.append("debug_toolbar")
#     MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
# except ImportError:
#     pass

# ==============================================================================
# 11. SECURITY (Relaxed for Dev)
# ==============================================================================
# В dev отключаем некоторые security-настройки для удобства.
# В prod они ВКЛЮЧЕНЫ в base.py и переопределяются в prod.py.

CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# ==============================================================================
# 12. TESTING HELPERS
# ==============================================================================
# Упрощаем парольную политику для тестовых пользователей

AUTH_PASSWORD_VALIDATORS = []

# ==============================================================================
# 13. DEVELOPER EXPERIENCE
# ==============================================================================
# Добавляем django-extensions для удобных команд:
# - python manage.py shell_plus (IPython + автоимпорт моделей)
# - python manage.py show_urls
# pip install django-extensions

try:
    import django_extensions  # noqa
    INSTALLED_APPS.append("django_extensions")
except ImportError:
    pass

# ==============================================================================
# FINAL CHECK
# ==============================================================================
# Выводим конфигурацию при старте, чтобы разработчик сразу видел,
# на какой БД и кэше он работает.

import sys

if "runserver" in sys.argv or "shell" in sys.argv:
    print("\n" + "=" * 70)
    print("🚀 AI Agents Platform — DEVELOPMENT MODE")
    print("=" * 70)
    print(f"📦 Database: {DATABASES['default']['ENGINE'].split('.')[-1]}")
    print(f"💾 Cache:    {CACHES['default']['BACKEND'].split('.')[-1]}")
    print(f"🔍 Debug:    {DEBUG}")
    print(f"📝 Log Level: {LOG_LEVEL}")
    print(f"🔒 CORS:     Allow All Origins")
    print("=" * 70 + "\n")