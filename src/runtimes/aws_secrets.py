"""Read approved AWS Secrets Manager values using the standard SDK identity chain."""

import asyncio
import json
import math
import re
from contextlib import suppress
from typing import Any

from django.conf import settings

from .secret_contracts import FetchedSecret, RuntimeConfigurationError, validate_secret_value

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+){1,3}-[0-9]{1,2}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_SECRET_BYTES = 65_536


def _create_client(region: str | None, timeout: float) -> Any:
    """Lazy optional imports; SDK identity discovery runs only inside a worker thread."""
    try:
        from boto3.session import Session
        from botocore.config import Config
    except ImportError:
        raise RuntimeConfigurationError("AWS secret support requires the aws extra") from None
    return Session().client(
        "secretsmanager",
        region_name=region,
        config=Config(
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def _binding(alias: str, binding: dict[str, Any]) -> tuple[str, str | None]:
    if (
        not isinstance(alias, str)
        or _ALIAS.fullmatch(alias) is None
        or not isinstance(binding, dict)
        or set(binding) - {"backend", "secret_id", "field"}
        or binding.get("backend") != "aws"
    ):
        raise RuntimeConfigurationError("The AWS secret binding is invalid")
    secret_id = binding.get("secret_id")
    field = binding.get("field")
    if not isinstance(secret_id, str) or len(secret_id) > 2048:
        raise RuntimeConfigurationError("The AWS secret binding is invalid")
    if secret_id.startswith("arn:"):
        parts = secret_id.split(":", 6)
        valid = (
            len(parts) == 7
            and re.fullmatch(r"aws(?:-[a-z]+)*", parts[1]) is not None
            and parts[2] == "secretsmanager"
            and _REGION.fullmatch(parts[3]) is not None
            and re.fullmatch(r"[0-9]{12}", parts[4]) is not None
            and parts[5] == "secret"
            and _SECRET_NAME.fullmatch(parts[6]) is not None
        )
    else:
        valid = _SECRET_NAME.fullmatch(secret_id) is not None
    if not valid or (
        "field" in binding and (not isinstance(field, str) or _FIELD.fullmatch(field) is None)
    ):
        raise RuntimeConfigurationError("The AWS secret binding is invalid")
    return secret_id, field


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate secret field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Non-finite secret JSON")


def _decode(response: Any, field: str | None) -> FetchedSecret:
    if not isinstance(response, dict) or "SecretBinary" in response:
        raise ValueError("Unsupported secret response")
    raw = response.get("SecretString")
    version = response.get("VersionId")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise ValueError("Invalid secret string")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError("Invalid secret version")
    stages = response.get("VersionStages")
    if "VersionStages" in response and (
        not isinstance(stages, list)
        or not 1 <= len(stages) <= 32
        or any(not isinstance(stage, str) or not 1 <= len(stage) <= 256 for stage in stages)
        or "AWSCURRENT" not in stages
    ):
        raise ValueError("The current secret version was not returned")
    value = raw
    if field is not None:
        payload = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        if not isinstance(payload, dict) or field not in payload:
            raise ValueError("The secret field is unavailable")
        value = payload[field]
    return FetchedSecret(value=validate_secret_value(value), version=version)


def _fetch_secret(region: str | None, timeout: float, secret_id: str, field: str | None):
    client = None
    try:
        client = _create_client(region, timeout)
        response = client.get_secret_value(SecretId=secret_id, VersionStage="AWSCURRENT")
        return _decode(response, field)
    except Exception:
        # Neither service error text nor a malformed secret value crosses this boundary.
        raise RuntimeConfigurationError("The AWS secret is unavailable") from None
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


class AwsSecretProvider:
    """One SDK client per refresh; blocking identity and I/O stay off the event loop."""

    def __init__(self, *, region: str | None, timeout: float):
        if region == "":
            region = None
        if region is not None and (
            not isinstance(region, str) or _REGION.fullmatch(region) is None
        ):
            raise RuntimeConfigurationError("AWS secret configuration is invalid")
        if (
            type(timeout) not in (float, int)
            or not math.isfinite(timeout)
            or not 0.1 <= timeout <= 30
        ):
            raise RuntimeConfigurationError("AWS secret configuration is invalid")
        self._region = region
        self._timeout = float(timeout)
        self._pending: set[asyncio.Task[FetchedSecret]] = set()
        self._closed = False

    @classmethod
    def from_settings(cls) -> "AwsSecretProvider":
        return cls(
            region=getattr(settings, "LLM_AWS_REGION", None),
            timeout=getattr(settings, "LLM_SECRET_TIMEOUT_SECONDS", 5.0),
        )

    def _finished(self, task: asyncio.Task[FetchedSecret]) -> None:
        self._pending.discard(task)
        if not task.cancelled():
            # Cancellation of a waiter must not leave an unobserved thread error.
            task.exception()

    async def fetch(self, alias: str, binding: dict[str, Any]) -> FetchedSecret:
        if self._closed:
            raise RuntimeConfigurationError("The AWS secret provider is closed")
        secret_id, field = _binding(alias, binding)
        task = asyncio.create_task(
            asyncio.to_thread(_fetch_secret, self._region, self._timeout, secret_id, field)
        )
        self._pending.add(task)
        task.add_done_callback(self._finished)
        # The SDK thread cannot be cancelled safely. It owns its client and
        # closes it in finally, even when the async waiter leaves first.
        return await asyncio.shield(task)

    async def close(self) -> None:
        self._closed = True
        if self._pending:
            await asyncio.shield(asyncio.gather(*tuple(self._pending), return_exceptions=True))
