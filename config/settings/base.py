"""
Base Django settings for AI Agents Platform.
Designed for Django 5.x + Django Nova + Async-First architecture.
"""
import logging
import os
import sys
from pathlib import Path
from typing import Any

import dj_database_url
import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict

# ==============================================================================
# 1. PATHS & ENVIRONMENT
# ==============================================================================

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# Environment Variables Management (Pydantic-powered)
class EnvSettings(BaseSettings):
    """
    Strict typing for environment variables.
    Reads from .env file or OS environment.
    """
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    # Core
    DEBUG: bool = False
    SECRET_KEY: str = "django-insecure-change-me-in-prod-via-env"
    ALLOWED_HOSTS: list[str] = ["*"]  # Restrict in prod.py
    ENVIRONMENT: str = "development"

    # Database
    DATABASE_URL: str = "postgres://postgres:postgres@localhost:5432/ai_agents"

    # Redis / Cache
    REDIS_URL: str = "redis://localhost:6379/0"

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    LOG_LEVEL: str = "INFO"

    # Nova Specific
    NOVA_CACHE_TTL: int = 300
    NOVA_TRACING_ENABLED: bool = True


env = EnvSettings()

# ==============================================================================
# 2. CORE DJANGO SETTINGS
# ==============================================================================

SECRET_KEY = env.SECRET_KEY
DEBUG = env.DEBUG
ALLOWED_HOSTS = env.ALLOWED_HOSTS

# Application definition
# Order matters: Nova apps should be loaded to patch/instrument correctly.
INSTALLED_APPS = [
    # Django Core
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # Required for JSONB/ArrayField

    # Third Party
    "rest_framework",
    "corsheaders",

    # Django Nova Ecosystem (Must be before domain apps to register hooks)
     "nova",

    # Domain Apps (DDD Structure)
    "src.core",
    "src.agents",
    "src.workflows",
    "src.runs",

]

MIDDLEWARE = [
    # Security & Session
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",

    # Observability (OpenTelemetry)
    # Injects trace_id/span_id into contextvars for structlog
    #"nova.tracing.middleware.OTelMiddleware",
]

# ASGI Application
# Points to our Hybrid Router (Django + FastAPI) defined in config/asgi.py
ASGI_APPLICATION = "config.asgi.application"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"  # Fallback, though we use ASGI

# ==============================================================================
# 3. DATABASE & CACHING (Async-Optimized)
# ==============================================================================

# PostgreSQL 15+ with psycopg3 (Async support)
DATABASES = {
    "default": dj_database_url.parse(
        env.DATABASE_URL,
        conn_max_age=600,  # Persistent connections для async ORM
        conn_health_checks=True,
    )
}

# Убедимся, что ENGINE точно указан (защита от пустых URL)
if not DATABASES["default"].get("ENGINE"):
    DATABASES["default"]["ENGINE"] = "django.db.backends.postgresql"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cache Configuration (Required for Django Nova Smart Cache)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.REDIS_URL,
        "OPTIONS": {
            "parser_class": "redis.connection.PythonParser",
            "pool_class": "redis.BlockingConnectionPool",
        },
        # Nova uses this TTL if not specified in @nova_model
        "TIMEOUT": env.NOVA_CACHE_TTL,
    }
}

# ==============================================================================
# 4. DJANGO NOVA CONFIGURATION
# ==============================================================================

# This dictionary drives the behavior of the Django Nova framework.
NOVA_SETTINGS = {
    # Validation
    "PYDANTIC_STRICT_MODE": True,  # Forbid extra fields in specs
    "STRICT_TYPES": True,  # Enforce type matching in ORM mapping

    # Caching
    "CACHE_ENABLED": True,
    "CACHE_BACKEND": "default",  # Use CACHES['default']
    "DEFAULT_CACHE_TTL": env.NOVA_CACHE_TTL,
    "INVALIDATION_STRATEGY": "signal",  # Auto-invalidate on post_save/delete

    # Tracing & Observability
    "TRACING_ENABLED": env.NOVA_TRACING_ENABLED,
    "TRACING_SAMPLE_RATE": 1.0,  # 100% sampling in dev, lower in prod
    "INCLUDE_SQL_QUERIES": True,  # Trace ORM calls

    # API Generation
    "AUTO_SCHEMA_GENERATION": True,  # Generate OpenAPI from Pydantic
}

# ==============================================================================
# 5. DJANGO REST FRAMEWORK (DRF)
# ==============================================================================

REST_FRAMEWORK = {
    # Use Django Nova's auto-serializer logic where possible
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        # Add Token/JWT auth here for API
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    # Async support for views
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle"
    ],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}


# ==============================================================================
# 6. LOGGING (Structlog + OpenTelemetry)
# ==============================================================================

# We completely replace Django's default logging with structlog for JSON output.
# This ensures every log line has trace_id/span_id if available.

def add_otel_context(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Injects OpenTelemetry context into every log record."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span.is_recording():
            ctx = span.get_span_context()
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except ImportError:
        pass
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        add_otel_context,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()  # Production-ready JSON logs
    ],
    # ИСПРАВЛЕНО: используем logging.INFO вместо structlog.INFO
    wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, env.LOG_LEVEL.upper(), logging.INFO)),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)

# Redirect standard library logging to structlog
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env.LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",  # Reduce Django noise
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "ERROR",  # Only log DB errors
            "propagate": False,
        },
    },
}

# ==============================================================================
# 7. INTERNATIONALIZATION & STATIC FILES
# ==============================================================================

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ==============================================================================
# 8. SECURITY & CORS
# ==============================================================================

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# CORS (Cross-Origin Resource Sharing)
# Configure strictly in prod.py
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",  # Frontend dev server
    "http://127.0.0.1:3000",
]