"""Production settings fail closed when required configuration is absent."""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

if len(env.SECRET_KEY) < 50 or env.SECRET_KEY.startswith("django-insecure"):
    raise ImproperlyConfigured("Production requires a random SECRET_KEY of at least 50 characters")
if not env.DATABASE_URL.startswith(("postgres://", "postgresql://")):
    raise ImproperlyConfigured("Production requires a PostgreSQL DATABASE_URL")
if not env.ALLOWED_HOSTS or any(
    host in {"*", "localhost", "127.0.0.1"} for host in env.ALLOWED_HOSTS
):
    raise ImproperlyConfigured("Production requires explicit non-local ALLOWED_HOSTS")
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = env.SECURE_SSL_REDIRECT
SECURE_HSTS_SECONDS = env.SECURE_HSTS_SECONDS
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
# Enable only when a trusted proxy overwrites this header and direct access is blocked.
if env.TRUST_PROXY_HEADERS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
