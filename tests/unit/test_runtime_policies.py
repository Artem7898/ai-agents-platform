"""Runtime endpoints and immutable snapshots fail closed before any HTTP call."""

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import ValidationError

from src.runtimes.policies import canonicalize_endpoint, validate_runtime_endpoint
from src.runtimes.schemas import LLMRuntimeSpec, RuntimeCapabilities, default_capabilities


def runtime_values(**changes):
    return {
        "id": "runtime-test",
        "owner_id": "1",
        "name": "Test runtime",
        "profile": "openai",
        "base_url": "https://api.openai.com/v1",
        "secret_ref": "test-cloud",
        "allowed_models": ["gpt-test"],
        "capabilities": default_capabilities("openai"),
        "revision": 1,
        **changes,
    }


def test_default_endpoint_policy_only_allows_official_openai(settings):
    del settings.LLM_ALLOWED_BASE_URLS
    assert validate_runtime_endpoint("https://api.openai.com/v1", "openai") == (
        "https://api.openai.com/v1"
    )
    with pytest.raises(DjangoValidationError, match="allowlist"):
        validate_runtime_endpoint("https://cloud.example.test/v1", "openai_compatible")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com.evil.test/v1",
        "https://evil-api.openai.com/v1",
        "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v11",
        "https://api.openai.com:8443/v1",
    ],
)
def test_allowlist_matches_entire_endpoint_not_host_or_path_prefix(settings, endpoint):
    settings.LLM_ALLOWED_BASE_URLS = ["https://api.openai.com/v1"]
    with pytest.raises(DjangoValidationError, match="allowlist"):
        validate_runtime_endpoint(endpoint, "openai")


def test_endpoint_canonicalization_preserves_exact_approved_service(settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://API.OPENAI.COM:443/v1/"]
    assert validate_runtime_endpoint("https://api.openai.com/v1/", "openai") == (
        "https://api.openai.com/v1"
    )
    assert canonicalize_endpoint("http://LOCALHOST:80/v1/") == "http://localhost/v1"
    assert canonicalize_endpoint("http://[::1]:11434/v1/") == "http://[::1]:11434/v1"


def test_plain_http_requires_ollama_and_operator_allowlist(settings):
    endpoint = "http://localhost:11434/v1"
    settings.LLM_ALLOWED_BASE_URLS = ["https://api.openai.com/v1"]
    with pytest.raises(DjangoValidationError, match="allowlist"):
        validate_runtime_endpoint(endpoint, "ollama")
    settings.LLM_ALLOWED_BASE_URLS = [endpoint]
    assert validate_runtime_endpoint(endpoint, "ollama") == endpoint
    for profile in ("openai", "openai_compatible"):
        with pytest.raises(DjangoValidationError, match="only for the Ollama"):
            validate_runtime_endpoint(endpoint, profile)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@api.openai.com/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v1?api_key=private",
        "https://api.openai.com/v1?",
        "https://api.openai.com/v1#fragment",
        "https://api.openai.com/v1#",
        "https://api.openai.com/v1\n",
        "https://api.openai.com/\x00v1",
        " https://api.openai.com/v1",
        "https://api.openai.com\\@evil.example/v1",
        "https://api.openai.com/v1/../internal",
        "https://api.openai.com/v1/./chat",
        "https://api.openai.com/v1//chat",
        "https://api.openai.com/v1/%2E%2E/internal",
        "https://api.openai.com/v1%2fextra",
        "https://api.openai.com/v1%5cextra",
        "https://api.openai.com:invalid/v1",
        "ftp://api.openai.com/v1",
        "file:///etc/passwd",
        "//api.openai.com/v1",
        "",
    ],
)
def test_ambiguous_or_credential_bearing_urls_are_rejected_even_if_configured(settings, endpoint):
    settings.LLM_ALLOWED_BASE_URLS = [endpoint]
    with pytest.raises(DjangoValidationError):
        validate_runtime_endpoint(endpoint, "openai")


@pytest.mark.parametrize("configured", [None, "https://api.openai.com/v1", {"url": "x"}, [None]])
def test_malformed_operator_allowlist_fails_closed(settings, configured):
    settings.LLM_ALLOWED_BASE_URLS = configured
    with pytest.raises(DjangoValidationError):
        validate_runtime_endpoint("https://api.openai.com/v1", "openai")


def test_unknown_profile_is_rejected(settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://api.openai.com/v1"]
    with pytest.raises(DjangoValidationError, match="Unknown runtime profile"):
        validate_runtime_endpoint("https://api.openai.com/v1", "arbitrary-provider")


@pytest.mark.parametrize("profile", ["openai", "ollama", "openai_compatible"])
def test_profile_defaults_require_explicit_model_capability_opt_in(profile):
    capabilities = default_capabilities(profile)
    assert not any(
        getattr(capabilities, field)
        for field in (
            "tools",
            "structured_outputs",
            "streaming",
            "usage",
            "strict_tools",
            "temperature",
        )
    )
    assert capabilities.token_limit_parameter == (
        "max_completion_tokens" if profile == "openai" else "max_tokens"
    )


def test_snapshots_and_nested_capabilities_are_immutable_and_reject_unknown_fields():
    spec = LLMRuntimeSpec.model_validate(runtime_values())
    assert spec.allowed_models == ("gpt-test",)
    assert spec.model_dump(mode="json")["allowed_models"] == ["gpt-test"]
    with pytest.raises(ValidationError, match="frozen_instance"):
        spec.enabled = False
    with pytest.raises(ValidationError, match="frozen_instance"):
        spec.capabilities.tools = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LLMRuntimeSpec.model_validate(runtime_values(api_key="never-snapshot-a-key"))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuntimeCapabilities.model_validate({"unsupported_feature": True})


def test_strict_tool_support_requires_tools_and_booleans_are_strict():
    with pytest.raises(ValidationError, match="requires tools"):
        RuntimeCapabilities(strict_tools=True)
    assert RuntimeCapabilities(tools=True, strict_tools=True).strict_tools is True
    with pytest.raises(ValidationError):
        RuntimeCapabilities.model_validate({"tools": "false"})
    with pytest.raises(ValidationError):
        LLMRuntimeSpec.model_validate(runtime_values(enabled="false"))


@pytest.mark.parametrize(
    "models", [[], [""], [" spaced "], ["duplicate", "duplicate"], ["x" * 129]]
)
def test_runtime_model_allowlist_requires_unique_nonempty_trimmed_names(models):
    with pytest.raises(ValidationError):
        LLMRuntimeSpec.model_validate(runtime_values(allowed_models=models))


@pytest.mark.parametrize(
    "secret_ref", ["Bearer sk-test-private", "key=value", "../../private", "", "x" * 129]
)
def test_secret_reference_is_an_alias_not_a_credential_expression(secret_ref):
    with pytest.raises(ValidationError):
        LLMRuntimeSpec.model_validate(runtime_values(secret_ref=secret_ref))


def test_valid_secret_alias_survives_snapshot_without_credential_values():
    spec = LLMRuntimeSpec.model_validate(runtime_values(secret_ref="team-a.openai-production"))
    assert spec.secret_ref == "team-a.openai-production"
    assert "api_key" not in spec.model_dump()
