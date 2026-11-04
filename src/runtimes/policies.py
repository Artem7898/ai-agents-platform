"""Endpoint policy is checked both when saving and before dispatching requests."""

import re
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError

PROFILES = {"openai", "ollama", "openai_compatible"}
DEFAULT_ALLOWED_BASE_URLS = ("https://api.openai.com/v1",)


def canonicalize_endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url or re.search(r"[\s\x00-\x1f\x7f\\]", base_url):
        raise ValidationError("The endpoint must be an HTTP(S) URL without whitespace.")
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.query or parsed.fragment or "?" in base_url or "#" in base_url:
            raise ValueError
        hostname = parsed.hostname.lower().encode("idna").decode("ascii")
        port = parsed.port
        if ":" in hostname:
            hostname = f"[{hostname}]"
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            hostname = f"{hostname}:{port}"
        path = parsed.path.rstrip("/")
        if "//" in path or any(part in {".", ".."} for part in path.split("/")):
            raise ValueError
        if re.search(r"%(?:2e|2f|5c)", path, flags=re.IGNORECASE):
            raise ValueError
        return urlunsplit((parsed.scheme, hostname, path, "", ""))
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(
            "The endpoint must not contain credentials, query parameters, fragments or ambiguous paths."
        ) from exc


def validate_runtime_endpoint(base_url: str, profile: str) -> str:
    if profile not in PROFILES:
        raise ValidationError("Unknown runtime profile.")
    canonical = canonicalize_endpoint(base_url)
    configured = getattr(settings, "LLM_ALLOWED_BASE_URLS", DEFAULT_ALLOWED_BASE_URLS)
    if not isinstance(configured, (list, tuple)):
        raise ValidationError("LLM_ALLOWED_BASE_URLS must be a list of approved endpoints.")
    allowed = {canonicalize_endpoint(value) for value in configured}
    if canonical not in allowed:
        raise ValidationError("This endpoint is not in the operator-managed LLM allowlist.")
    if canonical.startswith("http:") and profile != "ollama":
        raise ValidationError("Plain HTTP endpoints are allowed only for the Ollama profile.")
    return canonical


validate_endpoint = validate_runtime_endpoint
