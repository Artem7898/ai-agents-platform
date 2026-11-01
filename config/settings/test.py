"""Isolated test configuration; DATABASE_URL may select the CI PostgreSQL."""

from .base import *  # noqa: F403

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
NOVA_TRACING_ENABLED = False
