"""Compatibility alias: use DATABASE_URL to choose local PostgreSQL."""

from django.core.exceptions import ImproperlyConfigured

from .dev import *  # noqa: F403
from .dev import DATABASES

if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("dev_postgres requires a PostgreSQL DATABASE_URL")
