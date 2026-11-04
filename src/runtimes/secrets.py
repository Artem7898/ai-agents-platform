"""Configured aliases and loop-local, bounded secret-manager refreshes.

Provider bindings are server configuration. Runtime records never choose a
backend locator and explicit external bindings never fall back to local keys.
"""

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

import structlog
from django.conf import settings
from pydantic import SecretStr

from .secret_contracts import (
    FetchedSecret,
    RuntimeConfigurationError,
    SecretProvider,
    validate_secret_value,
)

__all__ = [
    "ResolvedCredential",
    "RuntimeConfigurationError",
    "close_runtime_secret_resolvers",
    "credential_is_current",
    "resolve_runtime_credential",
    "resolve_runtime_credential_async",
]

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROVIDER_SETTINGS = {
    "vault": (
        "LLM_VAULT_URL",
        "LLM_VAULT_MOUNT",
        "LLM_VAULT_NAMESPACE",
        "LLM_VAULT_TOKEN",
        "LLM_VAULT_TOKEN_FILE",
    ),
    "aws": ("LLM_AWS_REGION",),
    "gcp": ("LLM_GCP_PROJECT",),
}
_PROVIDER_ENVIRONMENT = {
    "vault": (),
    "aws": (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ),
    "gcp": (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_CONFIG",
        "CLOUDSDK_ACTIVE_CONFIG_NAME",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
    ),
}
log = structlog.get_logger(__name__)


@dataclass(frozen=True, repr=False)
class ResolvedCredential:
    value: SecretStr = field(repr=False)
    _version: str = field(repr=False)
    _generation: int = field(default=0, repr=False)
    _scope: str | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class _Configuration:
    alias: str
    backend: str
    binding: dict[str, Any] = field(repr=False)
    scope: str = field(repr=False)
    provider_scope: str = field(repr=False)
    ttl: float
    maximum: int
    timeout: float


@dataclass(repr=False)
class _CacheEntry:
    credential: ResolvedCredential = field(repr=False)
    expires_at: float | None


@dataclass(repr=False)
class _ProviderEntry:
    provider: SecretProvider = field(repr=False)
    scope: str = field(repr=False)
    leases: int = 0
    retired: bool = False


class _ConfigurationChanged(Exception):
    pass


def _normalize(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return {"secret_digest": hashlib.sha256(value.get_secret_value().encode()).hexdigest()}
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            _normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(encoded.encode()).hexdigest()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise RuntimeConfigurationError("Secret manager configuration is invalid") from None


def _check_alias(secret_ref: str) -> None:
    if not isinstance(secret_ref, str) or _ALIAS.fullmatch(secret_ref) is None:
        raise RuntimeConfigurationError("Runtime secret alias is invalid")


def _binding(secret_ref: str) -> dict[str, Any] | None:
    _check_alias(secret_ref)
    bindings = getattr(settings, "LLM_SECRET_BINDINGS", {})
    if not isinstance(bindings, dict):
        raise RuntimeConfigurationError("Secret manager configuration is invalid")
    if secret_ref not in bindings:
        return None
    binding = bindings[secret_ref]
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("backend"), str)
        or binding["backend"] not in _PROVIDER_SETTINGS
    ):
        raise RuntimeConfigurationError("Runtime secret binding is invalid")
    try:
        return copy.deepcopy(binding)
    except Exception:
        raise RuntimeConfigurationError("Runtime secret binding is invalid") from None


def _configuration(secret_ref: str) -> _Configuration | None:
    binding = _binding(secret_ref)
    if binding is None:
        return None
    backend = binding["backend"]
    ttl = getattr(settings, "LLM_SECRET_CACHE_TTL_SECONDS", 30)
    maximum = getattr(settings, "LLM_SECRET_CACHE_MAX_ENTRIES", 128)
    timeout = getattr(settings, "LLM_SECRET_TIMEOUT_SECONDS", 5)
    if (
        type(ttl) not in (int, float)
        or not math.isfinite(ttl)
        or not 0 <= ttl <= 300
        or type(maximum) is not int
        or not 1 <= maximum <= 1024
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 30
    ):
        raise RuntimeConfigurationError("Secret manager cache configuration is invalid")
    provider_settings = {
        name: getattr(settings, name, None) for name in _PROVIDER_SETTINGS[backend]
    }
    provider_settings["LLM_SECRET_TIMEOUT_SECONDS"] = getattr(
        settings, "LLM_SECRET_TIMEOUT_SECONDS", 5
    )
    provider_settings["environment_selectors"] = {
        name: os.environ.get(name) for name in _PROVIDER_ENVIRONMENT[backend]
    }
    provider_scope = _digest({"backend": backend, "settings": provider_settings})
    scope = _digest(
        {"provider": provider_scope, "binding": binding, "ttl": ttl, "maximum": maximum}
    )
    return _Configuration(
        secret_ref, backend, binding, scope, provider_scope, float(ttl), maximum, float(timeout)
    )


def resolve_runtime_credential(profile: str, secret_ref: str | None) -> ResolvedCredential:
    """Resolve local settings only; external access is always asynchronous."""
    if secret_ref is None:
        if profile != "ollama":
            raise RuntimeConfigurationError("Cloud runtimes require a configured secret alias")
        value = SecretStr("ollama-local-no-auth")
    else:
        if _binding(secret_ref) is not None:
            raise RuntimeConfigurationError("External secrets require asynchronous resolution")
        aliases = getattr(settings, "LLM_RUNTIME_SECRETS", {})
        configured = aliases.get(secret_ref) if isinstance(aliases, dict) else None
        if not isinstance(configured, SecretStr):
            raise RuntimeConfigurationError("Runtime secret alias is unavailable")
        value = validate_secret_value(configured.get_secret_value())
    return ResolvedCredential(
        value=value,
        _version=hashlib.sha256(value.get_secret_value().encode()).hexdigest(),
    )


def _create_provider(backend: str) -> SecretProvider:
    if backend == "vault":
        from .vault import VaultSecretProvider

        return VaultSecretProvider.from_settings()
    if backend == "aws":
        from .aws_secrets import AwsSecretProvider

        return AwsSecretProvider.from_settings()
    if backend == "gcp":
        from .gcp_secrets import GcpSecretProvider

        return GcpSecretProvider.from_settings()
    raise RuntimeConfigurationError("The secret manager backend is unavailable")


class RuntimeSecretResolver:
    """One event loop's positive cache and shared, configuration-fenced fetches."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._materials: OrderedDict[str, tuple[str, str, int]] = OrderedDict()
        self._inflight: dict[tuple[str, str], asyncio.Task[ResolvedCredential]] = {}
        self._providers: dict[str, _ProviderEntry] = {}
        self._generation = 0
        self._closed = False

    def _current(self, credential: ResolvedCredential, alias: str) -> bool:
        if self._closed:
            return False
        try:
            configuration = _configuration(alias)
        except RuntimeConfigurationError:
            return False
        entry = self._cache.get(alias)
        return bool(
            configuration is not None
            and entry is not None
            and credential._scope == configuration.scope
            and entry.credential._scope == configuration.scope
            and entry.credential._generation == credential._generation
            and entry.credential._version == credential._version
            and (entry.expires_at is None or entry.expires_at > time.monotonic())
        )

    async def resolve(self, alias: str) -> ResolvedCredential:
        for _ in range(4):
            if self._closed:
                raise RuntimeConfigurationError("The secret manager resolver is closed")
            configuration = _configuration(alias)
            if configuration is None:
                raise _ConfigurationChanged
            entry = self._cache.get(alias)
            if configuration.ttl > 0 and entry and self._current(entry.credential, alias):
                self._cache.move_to_end(alias)
                return entry.credential
            self._cache.pop(alias, None)
            key = (alias, configuration.scope)
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= configuration.maximum:
                    raise RuntimeConfigurationError("Too many secret refreshes are in progress")
                task = asyncio.create_task(self._fetch(configuration))
                self._inflight[key] = task
                task.add_done_callback(self._consume_exception)
            try:
                async with asyncio.timeout(configuration.timeout):
                    credential = await asyncio.shield(task)
            except _ConfigurationChanged:
                continue
            except TimeoutError:
                log.warning(
                    "llm_secret_refresh_failed",
                    backend=configuration.backend,
                    alias=configuration.alias,
                    outcome="timeout",
                )
                raise RuntimeConfigurationError("Runtime secret resolution timed out") from None
            if self._current(credential, alias):
                return credential
        raise RuntimeConfigurationError("Secret manager configuration changed during resolution")

    @staticmethod
    def _consume_exception(task: asyncio.Task[ResolvedCredential]) -> None:
        if not task.cancelled():
            task.exception()

    async def _provider(self, configuration: _Configuration) -> _ProviderEntry:
        entry = self._providers.get(configuration.backend)
        if entry is not None and entry.scope == configuration.provider_scope:
            entry.leases += 1
            return entry
        provider = _create_provider(configuration.backend)
        replacement = _ProviderEntry(provider, configuration.provider_scope, leases=1)
        self._providers[configuration.backend] = replacement
        if entry is not None:
            entry.retired = True
            if not entry.leases:
                with suppress(Exception):
                    await entry.provider.close()
        return replacement

    async def _fetch(self, configuration: _Configuration) -> ResolvedCredential:
        key = (configuration.alias, configuration.scope)
        provider_entry = None
        fetch_deadline = asyncio.get_running_loop().time() + configuration.timeout
        try:
            current = _configuration(configuration.alias)
            if current is None or current.scope != configuration.scope:
                raise _ConfigurationChanged
            provider_entry = await self._provider(configuration)
            fetched = await provider_entry.provider.fetch(
                configuration.alias, configuration.binding
            )
            if asyncio.get_running_loop().time() >= fetch_deadline:
                raise RuntimeConfigurationError("The secret manager response arrived too late")
            if not isinstance(fetched, FetchedSecret) or not isinstance(fetched.value, SecretStr):
                raise RuntimeConfigurationError("The secret manager returned an invalid value")
            value = validate_secret_value(fetched.value.get_secret_value())
            if not isinstance(fetched.version, str) or not 1 <= len(fetched.version) <= 2048:
                raise RuntimeConfigurationError("The secret manager returned an invalid version")
            current = _configuration(configuration.alias)
            if self._closed or current is None or current.scope != configuration.scope:
                raise _ConfigurationChanged
            version = _digest({"value": value, "version": fetched.version})
            previous = self._materials.get(configuration.alias)
            if previous and previous[:2] == (configuration.scope, version):
                generation = previous[2]
            else:
                self._generation += 1
                generation = self._generation
            credential = ResolvedCredential(value, version, generation, configuration.scope)
            deadline = time.monotonic() + configuration.ttl if configuration.ttl else None
            self._cache[configuration.alias] = _CacheEntry(credential, deadline)
            self._cache.move_to_end(configuration.alias)
            self._materials[configuration.alias] = (configuration.scope, version, generation)
            self._materials.move_to_end(configuration.alias)
            while len(self._cache) > configuration.maximum:
                self._cache.popitem(last=False)
            while len(self._materials) > configuration.maximum:
                self._materials.popitem(last=False)
            log.info(
                "llm_secret_rotated"
                if previous and previous[1] != version
                else "llm_secret_refreshed",
                backend=configuration.backend,
                alias=configuration.alias,
                outcome="success",
            )
            return credential
        except _ConfigurationChanged:
            raise
        except Exception:
            existing = self._cache.get(configuration.alias)
            if existing and existing.credential._scope == configuration.scope:
                self._cache.pop(configuration.alias, None)
            log.warning(
                "llm_secret_refresh_failed",
                backend=configuration.backend,
                alias=configuration.alias,
                outcome="unavailable",
            )
            raise RuntimeConfigurationError("Runtime secret could not be resolved") from None
        finally:
            if self._inflight.get(key) is asyncio.current_task():
                self._inflight.pop(key, None)
            if provider_entry is not None:
                provider_entry.leases -= 1
                if provider_entry.retired and provider_entry.leases == 0:
                    with suppress(Exception):
                        await provider_entry.provider.close()

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        self._materials.clear()
        providers = list(self._providers.values())
        self._providers.clear()
        for entry in providers:
            with suppress(Exception):
                await entry.provider.close()


_resolvers: WeakKeyDictionary[asyncio.AbstractEventLoop, RuntimeSecretResolver] = (
    WeakKeyDictionary()
)


async def resolve_runtime_credential_async(
    profile: str, secret_ref: str | None
) -> ResolvedCredential:
    if secret_ref is None or _binding(secret_ref) is None:
        return resolve_runtime_credential(profile, secret_ref)
    loop = asyncio.get_running_loop()
    resolver = _resolvers.get(loop)
    if resolver is None:
        resolver = RuntimeSecretResolver()
        _resolvers[loop] = resolver
    try:
        return await resolver.resolve(secret_ref)
    except _ConfigurationChanged:
        return resolve_runtime_credential(profile, secret_ref)


def credential_is_current(
    credential: ResolvedCredential, profile: str, secret_ref: str | None
) -> bool:
    """An I/O-free fencing check for the SDK pool's short critical section."""
    try:
        if secret_ref is None or _binding(secret_ref) is None:
            current = resolve_runtime_credential(profile, secret_ref)
            return credential._scope is None and current._version == credential._version
        resolver = _resolvers.get(asyncio.get_running_loop())
        return resolver is not None and resolver._current(credential, secret_ref)
    except (RuntimeConfigurationError, RuntimeError):
        return False


async def close_runtime_secret_resolvers() -> None:
    resolver = _resolvers.pop(asyncio.get_running_loop(), None)
    if resolver is not None:
        await resolver.close()
