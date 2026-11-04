"""External backend configuration stays bounded and secret-safe before use."""

import pytest
from pydantic import SecretStr, ValidationError

from config.settings.base import LOGGING, EnvSettings


def test_all_three_backends_can_be_configured_without_static_secret_values():
    bindings = {
        "vault-cloud": {"backend": "vault", "path": "platform/cloud", "field": "api_key"},
        "aws-cloud": {"backend": "aws", "secret_id": "platform/cloud"},
        "gcp-cloud": {"backend": "gcp", "project": "123456789012", "secret_id": "cloud"},
    }
    config = EnvSettings(_env_file=None, LLM_SECRET_BINDINGS=bindings)
    assert config.LLM_SECRET_BINDINGS == bindings
    assert config.LLM_SECRET_CACHE_TTL_SECONDS == 30
    assert config.LLM_SECRET_CACHE_MAX_ENTRIES == 128


@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_SECRET_CACHE_TTL_SECONDS", -1),
        ("LLM_SECRET_CACHE_TTL_SECONDS", 301),
        ("LLM_SECRET_CACHE_MAX_ENTRIES", 0),
        ("LLM_SECRET_CACHE_MAX_ENTRIES", 1025),
        ("LLM_SECRET_TIMEOUT_SECONDS", 0.01),
        ("LLM_SECRET_TIMEOUT_SECONDS", 31),
    ],
)
def test_secret_refresh_limits_reject_unbounded_configuration(name, value):
    with pytest.raises(ValidationError):
        EnvSettings(_env_file=None, **{name: value})


@pytest.mark.parametrize(
    "bindings",
    [
        {"../private": {"backend": "vault"}},
        {"alias": {}},
        {"alias": {"backend": "unsupported"}},
    ],
)
def test_invalid_binding_errors_do_not_print_bootstrap_credentials(bindings):
    marker = "private-vault-bootstrap-value"
    with pytest.raises(ValidationError) as error:
        EnvSettings(_env_file=None, LLM_SECRET_BINDINGS=bindings, LLM_VAULT_TOKEN=marker)
    assert marker not in str(error.value)


def test_zero_ttl_is_explicitly_available_and_vault_token_is_masked():
    marker = "private-vault-bootstrap-value"
    config = EnvSettings(_env_file=None, LLM_SECRET_CACHE_TTL_SECONDS=0, LLM_VAULT_TOKEN=marker)
    assert config.LLM_SECRET_CACHE_TTL_SECONDS == 0
    assert isinstance(config.LLM_VAULT_TOKEN, SecretStr)
    assert marker not in repr(config.LLM_VAULT_TOKEN)


def test_vendor_wire_debug_logging_is_disabled_for_secret_backends():
    for name in ("httpx", "httpcore", "botocore", "google.auth", "google.cloud.secretmanager_v1"):
        assert LOGGING["loggers"][name]["level"] == "WARNING"
        assert LOGGING["loggers"][name]["propagate"] is False
