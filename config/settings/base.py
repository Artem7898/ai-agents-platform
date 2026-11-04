"""Shared settings. Production requirements are validated in prod.py."""

from pathlib import Path
from typing import Any

import dj_database_url
import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.types import EventDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    SECRET_KEY: str = "django-insecure-local-development-only"
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
    ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1"]
    CORS_ALLOWED_ORIGINS: list[str] = []
    CSRF_TRUSTED_ORIGINS: list[str] = []
    REDIS_URL: str = ""
    LOG_LEVEL: str = "INFO"
    NOVA_TRACING_ENABLED: bool = False
    SECURE_SSL_REDIRECT: bool = True
    SECURE_HSTS_SECONDS: int = 3600
    TRUST_PROXY_HEADERS: bool = False
    KNOWLEDGE_BASE_SEARCH_URL: str = ""
    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_BASE_URL: str | None = None
    LLM_ALLOWED_BASE_URLS: list[str] = ["https://api.openai.com/v1"]
    LLM_RUNTIME_SECRETS: dict[str, SecretStr] = {}
    OUTBOX_LEASE_SECONDS: float = Field(default=600, ge=3, le=86400)
    OUTBOX_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=100)
    LLM_TRANSPORT_MAX_RETRIES: int = Field(default=2, ge=0, le=2)
    LLM_RETRY_BASE_SECONDS: float = Field(default=0.25, ge=0, le=10)
    LLM_RESERVATION_OVERHEAD_TOKENS: int = Field(default=512, ge=0, le=8192)


env = EnvSettings()
SECRET_KEY = env.SECRET_KEY
KNOWLEDGE_BASE_SEARCH_URL = env.KNOWLEDGE_BASE_SEARCH_URL
OPENAI_API_KEY = env.OPENAI_API_KEY.get_secret_value() if env.OPENAI_API_KEY else None
OPENAI_BASE_URL = env.OPENAI_BASE_URL
LLM_ALLOWED_BASE_URLS = env.LLM_ALLOWED_BASE_URLS
LLM_RUNTIME_SECRETS = env.LLM_RUNTIME_SECRETS
OUTBOX_LEASE_SECONDS = env.OUTBOX_LEASE_SECONDS
OUTBOX_MAX_ATTEMPTS = env.OUTBOX_MAX_ATTEMPTS
LLM_TRANSPORT_MAX_RETRIES = env.LLM_TRANSPORT_MAX_RETRIES
LLM_RETRY_BASE_SECONDS = env.LLM_RETRY_BASE_SECONDS
LLM_RESERVATION_OVERHEAD_TOKENS = env.LLM_RESERVATION_OVERHEAD_TOKENS
DEBUG = False
ALLOWED_HOSTS = env.ALLOWED_HOSTS
INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "nova",
    "src.core",
    "src.agents",
    "src.runtimes",
    "src.usage",
    "src.workflows",
    "src.runs",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"
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
            ]
        },
    }
]
# Persistent Django connections should be disabled in ASGI mode. The database
# or an external pooler provides connection pooling when required.
DATABASES = {
    "default": dj_database_url.parse(env.DATABASE_URL, conn_max_age=0, conn_health_checks=True)
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
if env.REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": env.REDIS_URL,
            "TIMEOUT": 300,
        }
    }
NOVA_CACHE_BACKEND = "memory"
NOVA_TRACING_ENABLED = env.NOVA_TRACING_ENABLED
NOVA_SETTINGS = {"TRACING_ENABLED": NOVA_TRACING_ENABLED}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "30/minute", "user": "300/minute"},
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}


def add_otel_context(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    from opentelemetry import trace

    span = trace.get_current_span()
    if span.is_recording():
        context = span.get_span_context()
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_otel_context,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env.LOG_LEVEL},
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOWED_ORIGINS = env.CORS_ALLOWED_ORIGINS
CSRF_TRUSTED_ORIGINS = env.CSRF_TRUSTED_ORIGINS
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
