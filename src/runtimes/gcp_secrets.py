"""Google Secret Manager reads through ADC, outside the asyncio event loop."""

import asyncio
import importlib
import json
import math
import re
import threading
from typing import Any

from django.conf import settings
from pydantic import SecretStr

from .secret_contracts import FetchedSecret, RuntimeConfigurationError, validate_secret_value

_PROJECT = re.compile(r"^[1-9][0-9]{0,19}$")
_SECRET_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_VERSION = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_PAYLOAD_BYTES = 65_536
_UNAVAILABLE = "Google Secret Manager secret is unavailable"


def _create_client() -> Any:
    """Leave credential discovery and refresh to the SDK's normal ADC chain."""
    try:
        sdk = importlib.import_module("google.cloud.secretmanager_v1")
    except ImportError:
        raise RuntimeConfigurationError(
            "Google Secret Manager requires the gcp optional dependency"
        ) from None
    return sdk.SecretManagerServiceClient()


def _checksum(data: bytes) -> int:
    try:
        crc32c = importlib.import_module("google_crc32c")
    except ImportError:
        raise RuntimeConfigurationError(
            "Google Secret Manager requires the gcp optional dependency"
        ) from None
    return crc32c.value(data)


def _checksum_present(payload: Any) -> bool:
    # Proto-plus exposes an unset optional int64 as zero; its presence must be
    # checked separately so a missing CRC cannot masquerade as a valid zero.
    converter = getattr(type(payload), "pb", None)
    if callable(converter):
        message: Any = converter(payload)
        return message.HasField("data_crc32c") is True
    has_field = getattr(payload, "HasField", None)
    return has_field("data_crc32c") is True if callable(has_field) else True


def _secret_value(data: bytes, field: str | None) -> SecretStr:
    try:
        value = data.decode("utf-8")
        if field is not None:
            decoded = json.loads(value)
            value = decoded.get(field) if isinstance(decoded, dict) else None
        if not isinstance(value, str) or value.splitlines() != [value]:
            raise ValueError("Invalid secret value")
        return validate_secret_value(value)
    except (RuntimeConfigurationError, UnicodeError, ValueError, TypeError, RecursionError):
        raise RuntimeConfigurationError(_UNAVAILABLE) from None


class GcpSecretProvider:
    """A reusable synchronous SDK client with asynchronous lifecycle methods.

    Only project numbers are accepted, so the returned canonical resource can be
    compared exactly with the approved server binding. A client is closed after
    active thread operations finish, including a cancelled async waiter's read.
    """

    def __init__(self, *, project: str = "", timeout: float = 5.0):
        if not isinstance(project, str) or (project and _PROJECT.fullmatch(project) is None):
            raise RuntimeConfigurationError("GCP requires a numeric project number")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= 30
        ):
            raise RuntimeConfigurationError("Secret Manager timeout configuration is invalid")
        self._project = project
        self._timeout = float(timeout)
        self._condition = threading.Condition()
        self._client: Any = None
        self._active = 0
        self._closed = False
        self._close_done = False

    @classmethod
    def from_settings(cls) -> "GcpSecretProvider":
        return cls(
            project=getattr(settings, "LLM_GCP_PROJECT", "") or "",
            timeout=getattr(settings, "LLM_SECRET_TIMEOUT_SECONDS", 5.0),
        )

    def _binding(self, alias: str, binding: dict[str, Any]) -> tuple[str, str | None]:
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise RuntimeConfigurationError("Secret alias configuration is invalid")
        if not isinstance(binding, dict) or set(binding) - {
            "backend",
            "project",
            "secret_id",
            "field",
        }:
            raise RuntimeConfigurationError("GCP secret binding is invalid")
        if binding.get("backend") != "gcp":
            raise RuntimeConfigurationError("GCP secret binding is invalid")
        project = binding.get("project", self._project)
        if not isinstance(project, str) or _PROJECT.fullmatch(project) is None:
            raise RuntimeConfigurationError("GCP requires a numeric project number")
        secret_id = binding.get("secret_id")
        field = binding.get("field")
        if not isinstance(secret_id, str) or _SECRET_ID.fullmatch(secret_id) is None:
            raise RuntimeConfigurationError("GCP secret binding is invalid")
        if field is not None and (not isinstance(field, str) or _FIELD.fullmatch(field) is None):
            raise RuntimeConfigurationError("GCP secret binding is invalid")
        return f"projects/{project}/secrets/{secret_id}", field

    def _acquire_client(self) -> Any:
        with self._condition:
            if self._closed:
                raise RuntimeConfigurationError("Secret Manager provider is closed")
            if self._client is None:
                self._client = _create_client()
            self._active += 1
            return self._client

    def _fetch(self, resource: str, field: str | None) -> FetchedSecret:
        client = self._acquire_client()
        try:
            response = client.access_secret_version(
                request={"name": resource + "/versions/latest"},
                retry=None,
                timeout=self._timeout,
            )
            name = getattr(response, "name", None)
            prefix = resource + "/versions/"
            if not isinstance(name, str) or not name.startswith(prefix):
                raise RuntimeConfigurationError(_UNAVAILABLE)
            version = name[len(prefix) :]
            if _VERSION.fullmatch(version) is None:
                raise RuntimeConfigurationError(_UNAVAILABLE)
            payload = getattr(response, "payload", None)
            data = getattr(payload, "data", None)
            checksum = getattr(payload, "data_crc32c", None)
            if (
                not isinstance(data, bytes)
                or not data
                or len(data) > _MAX_PAYLOAD_BYTES
                or type(checksum) is not int
                or not 0 <= checksum <= 0xFFFFFFFF
                or not _checksum_present(payload)
                or _checksum(data) != checksum
            ):
                raise RuntimeConfigurationError(_UNAVAILABLE)
            return FetchedSecret(value=_secret_value(data, field), version=version)
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    async def fetch(self, alias: str, binding: dict[str, Any]) -> FetchedSecret:
        resource, field = self._binding(alias, binding)
        try:
            return await asyncio.to_thread(self._fetch, resource, field)
        except RuntimeConfigurationError:
            raise
        except Exception:
            raise RuntimeConfigurationError(_UNAVAILABLE) from None

    def _close(self) -> None:
        with self._condition:
            if self._closed:
                self._condition.wait_for(lambda: self._close_done)
                return
            self._closed = True
            self._condition.wait_for(lambda: self._active == 0)
            client, self._client = self._client, None
        try:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
                else:
                    client.transport.close()
        except Exception:
            # A shutdown failure must not expose an SDK/vendor exception.
            pass
        finally:
            with self._condition:
                self._close_done = True
                self._condition.notify_all()

    async def close(self) -> None:
        await asyncio.to_thread(self._close)
