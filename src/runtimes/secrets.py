"""Resolve configured aliases without exposing credentials in runtime snapshots."""

import hashlib
from dataclasses import dataclass, field

from django.conf import settings
from pydantic import SecretStr


class RuntimeConfigurationError(ValueError):
    """A runtime cannot be used with the current server configuration."""


@dataclass(frozen=True, repr=False)
class ResolvedCredential:
    value: SecretStr = field(repr=False)
    # This private identifier is only used to invalidate cached SDK clients.
    _version: str = field(repr=False)


def resolve_runtime_credential(profile: str, secret_ref: str | None) -> ResolvedCredential:
    """Only configured aliases are accepted; secret_ref is never an env variable name."""
    if secret_ref is None:
        if profile != "ollama":
            raise RuntimeConfigurationError("Cloud runtimes require a configured secret alias")
        value = SecretStr("ollama-local-no-auth")
    else:
        aliases = getattr(settings, "LLM_RUNTIME_SECRETS", {})
        configured = aliases.get(secret_ref)
        if not isinstance(configured, SecretStr) or not configured.get_secret_value().strip():
            raise RuntimeConfigurationError("Runtime secret alias is unavailable")
        value = configured
    return ResolvedCredential(
        value=value,
        _version=hashlib.sha256(value.get_secret_value().encode()).hexdigest(),
    )
