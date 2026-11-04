"""Secret backend values shared without importing Django or provider SDKs."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import SecretStr


class RuntimeConfigurationError(ValueError):
    """A runtime cannot be used with the current server configuration."""


def validate_secret_value(value: Any) -> SecretStr:
    """Accept a bounded, nonempty single-line credential without echoing it."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.splitlines() != [value]
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise RuntimeConfigurationError("The secret value is invalid")
    try:
        valid_size = len(value.encode("utf-8")) <= 16_384
    except UnicodeError:
        valid_size = False
    if not valid_size:
        raise RuntimeConfigurationError("The secret value is invalid")
    return SecretStr(value)


@dataclass(frozen=True, repr=False)
class FetchedSecret:
    value: SecretStr = field(repr=False)
    version: str = field(repr=False)


class SecretProvider(Protocol):
    async def fetch(self, alias: str, binding: dict[str, Any]) -> FetchedSecret: ...

    async def close(self) -> None: ...
