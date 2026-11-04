"""Read explicitly configured Vault KV v2 values without retaining a secret cache."""

import asyncio
import json
import os
import re
import stat
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from django.conf import settings
from pydantic import SecretStr

from .secret_contracts import FetchedSecret, RuntimeConfigurationError, validate_secret_value

_MAX_SECRET_BYTES = 16_384
_MAX_RESPONSE_BYTES = 65_536
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _unavailable() -> RuntimeConfigurationError:
    return RuntimeConfigurationError("Vault secret is unavailable")


def _configuration_error() -> RuntimeConfigurationError:
    return RuntimeConfigurationError("Vault secret configuration is invalid")


def _path(value: Any, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise _configuration_error()
    parts = value.split("/")
    if len(parts) > 16 or any(
        part in {".", ".."} or _SEGMENT.fullmatch(part) is None for part in parts
    ):
        raise _configuration_error()
    return "/".join(quote(part, safe="") for part in parts)


def _address(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise _configuration_error()
    if (
        any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or "\\" in value
    ):
        raise _configuration_error()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port not in (None, 443):
            host = f"{host}:{port}"
        prefix = parsed.path.rstrip("/")
        if "//" in parsed.path:
            raise ValueError
        if prefix:
            if not prefix.startswith("/"):
                raise ValueError
            prefix = "/" + _path(prefix[1:])
        return urlunsplit(("https", host, prefix, "", ""))
    except (ValueError, UnicodeError):
        raise _configuration_error() from None


def _create_http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=False,
        verify=True,
    )


def _read_token_file(path: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _unavailable()
        with os.fdopen(descriptor, "rb", closefd=False) as token_file:
            raw = token_file.read(_MAX_SECRET_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_SECRET_BYTES:
        raise _unavailable()
    return raw.decode("utf-8").rstrip("\r\n")


class VaultSecretProvider:
    """Lease-safe resolution is owned by the resolver; this adapter only fetches."""

    def __init__(
        self,
        *,
        url: str,
        mount: str = "secret",
        namespace: str = "",
        token: SecretStr | None = None,
        token_file: str | None = None,
        timeout: float = 5.0,
    ):
        self._url = _address(url)
        self._mount = _path(mount, maximum=64)
        if "/" in self._mount:
            raise _configuration_error()
        self._namespace = _path(namespace, maximum=256) if namespace else ""
        if (token is None) == (token_file is None):
            raise _configuration_error()
        if token is not None and not isinstance(token, SecretStr):
            raise _configuration_error()
        if token_file is not None and (
            not isinstance(token_file, str)
            or not token_file
            or len(token_file) > 4096
            or "\x00" in token_file
        ):
            raise _configuration_error()
        if type(timeout) not in (float, int) or not 0.1 <= timeout <= 30:
            raise _configuration_error()
        self._token = token
        self._token_file = token_file
        self._timeout = float(timeout)
        self._closed = False
        self._client = _create_http_client(self._timeout)

    @classmethod
    def from_settings(cls) -> "VaultSecretProvider":
        return cls(
            url=getattr(settings, "LLM_VAULT_URL", ""),
            mount=getattr(settings, "LLM_VAULT_MOUNT", "secret"),
            namespace=getattr(settings, "LLM_VAULT_NAMESPACE", ""),
            token=getattr(settings, "LLM_VAULT_TOKEN", None),
            token_file=getattr(settings, "LLM_VAULT_TOKEN_FILE", None),
            timeout=getattr(settings, "LLM_SECRET_TIMEOUT_SECONDS", 5.0),
        )

    async def _request_token(self) -> str:
        if self._token_file is not None:
            value = await asyncio.to_thread(_read_token_file, self._token_file)
        elif self._token is not None:
            value = self._token.get_secret_value()
        else:
            raise _unavailable()
        token = validate_secret_value(value).get_secret_value()
        # Vault tokens are transported as HTTP headers, which must be ASCII.
        if not token.isascii():
            raise _unavailable()
        return token

    async def fetch(self, alias: str, binding: dict[str, Any]) -> FetchedSecret:
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise _configuration_error()
        if (
            not isinstance(binding, dict)
            or set(binding) != {"backend", "path", "field"}
            or binding.get("backend") != "vault"
        ):
            raise _configuration_error()
        path = _path(binding["path"])
        field = binding["field"]
        if not isinstance(field, str) or _SEGMENT.fullmatch(field) is None or field in {".", ".."}:
            raise _configuration_error()
        if self._closed:
            raise _unavailable()
        try:
            # HTTPX limits individual I/O phases. The complete fetch also needs
            # a deadline so repeated small chunks cannot keep it alive forever.
            async with asyncio.timeout(self._timeout):
                return await self._fetch(path, field)
        except Exception:
            # Never propagate a response body, locator, bootstrap token or secret.
            # BaseException subclasses, including cancellation, retain control flow.
            raise _unavailable() from None

    async def _fetch(self, path: str, field: str) -> FetchedSecret:
        headers = {
            "X-Vault-Token": await self._request_token(),
            "Accept-Encoding": "identity",
        }
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        url = f"{self._url}/v1/{self._mount}/data/{path}"
        raw = bytearray()
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                raise _unavailable()
            # Do not decompress an unbounded response before checking its
            # size. KV JSON is small and does not need compressed transfer.
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise _unavailable()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise _unavailable()
        payload = json.loads(raw)
        envelope = payload["data"]
        metadata = envelope["metadata"]
        version = metadata["version"]
        if (
            type(version) is not int
            or not 1 <= version < 2**31
            or metadata["destroyed"] is not False
            or metadata["deletion_time"] != ""
        ):
            raise _unavailable()
        value = validate_secret_value(envelope["data"][field])
        return FetchedSecret(value=value, version=str(version))

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()
