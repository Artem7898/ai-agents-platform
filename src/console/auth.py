"""CSRF-protected browser authentication; API credentials never reach JavaScript."""

import hashlib
import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig, ValidationError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

_MAX_BODY = 20_480
_LOGIN_WINDOW = 300
_LOGIN_USER_LIMIT = 10
_LOGIN_IP_LIMIT = 50


def _json_error(code, status=400):
    return JsonResponse({"code": code}, status=status)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _body(request, allowed):
    if request.content_type != "application/json":
        raise ValueError
    declared = request.META.get("CONTENT_LENGTH")
    if declared not in (None, "") and not 0 <= int(declared) <= _MAX_BODY:
        raise ValueError
    # Do not allocate an unbounded body when Content-Length is absent. Reading
    # one extra byte distinguishes an exact-size JSON document from overflow.
    raw = request.read(_MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        raise ValueError
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except RecursionError:
        raise ValueError from None
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError
    return value


def _authenticated(request):
    return request.user.is_authenticated and request.user.is_active


def _session_payload(request):
    authenticated = bool(_authenticated(request))
    user = request.user
    return {
        "authenticated": authenticated,
        "csrf_token": get_token(request),
        "user": {
            "id": user.pk,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_staff": user.is_staff,
        }
        if authenticated
        else None,
        "billing": {"enabled": settings.BILLING_ENABLED, "mode": settings.BILLING_MODE},
        "environment": "development" if settings.DEBUG else "production",
        "timezone": settings.TIME_ZONE,
        "capabilities": {
            "knowledge_search_configured": bool(settings.KNOWLEDGE_BASE_SEARCH_URL),
        },
    }


def _require_session(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _authenticated(request):
            return _json_error("authentication_required", 401)
        return view(request, *args, **kwargs)

    return wrapped


def _login_keys(request, username):
    # REMOTE_ADDR comes from the deployment, not an arbitrary forwarded header.
    # Cache keys contain hashes rather than credentials or user-provided strings.
    address = request.META.get("REMOTE_ADDR", "unknown")
    return (
        "console-login-ip:" + hashlib.sha256(str(address).encode()).hexdigest(),
        "console-login-user:" + hashlib.sha256(username.casefold().encode()).hexdigest(),
    )


def _increment(key):
    cache.add(key, 0, timeout=_LOGIN_WINDOW)
    try:
        return cache.incr(key)
    except ValueError:
        # A fixed-window key may expire between add and incr.
        cache.add(key, 0, timeout=_LOGIN_WINDOW)
        return cache.incr(key)


@never_cache
@require_GET
def session(request):
    return JsonResponse(_session_payload(request))


@never_cache
@require_POST
@csrf_protect
def sign_in(request):
    try:
        values = _body(request, ("username", "password"))
        username, password = values.get("username"), values.get("password")
        if (
            not isinstance(username, str)
            or not 1 <= len(username) <= 150
            or not isinstance(password, str)
            or not 1 <= len(password) <= 1024
        ):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return _json_error("invalid_login")
    ip_key, user_key = _login_keys(request, username)
    try:
        limited = _increment(ip_key) > _LOGIN_IP_LIMIT or _increment(user_key) > _LOGIN_USER_LIMIT
    except Exception:
        return _json_error("authentication_unavailable", 503)
    if limited:
        response = _json_error("login_rate_limited", 429)
        response["Retry-After"] = str(_LOGIN_WINDOW)
        return response
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _json_error("invalid_credentials", 401)
    login(request, user)
    try:
        cache.delete(user_key)
    except Exception:
        # Authentication was already throttled successfully before validation.
        pass
    return JsonResponse(_session_payload(request))


@never_cache
@require_POST
@csrf_protect
@_require_session
def sign_out(request):
    logout(request)
    return JsonResponse(_session_payload(request))


@never_cache
@require_POST
@csrf_protect
@_require_session
def password(request):
    try:
        values = _body(request, ("old_password", "new_password"))
        old, new = values.get("old_password"), values.get("new_password")
        if (
            not isinstance(old, str)
            or not 1 <= len(old) <= 1024
            or not isinstance(new, str)
            or not 1 <= len(new) <= 1024
            or not request.user.check_password(old)
        ):
            raise ValueError
        validate_password(new, user=request.user)
    except (ValueError, UnicodeError, ValidationError, RecursionError, RequestDataTooBig):
        return _json_error("password_invalid")
    request.user.set_password(new)
    request.user.save(update_fields=["password"])
    update_session_auth_hash(request, request.user)
    return JsonResponse({"updated": True, "csrf_token": get_token(request)})


@never_cache
@require_POST
@csrf_protect
@_require_session
def profile(request):
    try:
        values = _body(request, ("first_name", "last_name"))
        if not values or any(
            not isinstance(value, str) or len(value) > 150 for value in values.values()
        ):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return _json_error("invalid_profile")
    for name, value in values.items():
        setattr(request.user, name, value.strip())
    request.user.save(update_fields=list(values))
    return JsonResponse(_session_payload(request))
