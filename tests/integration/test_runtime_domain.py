"""Runtime saves validate configuration and freeze credential-free snapshots."""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from src.runtimes.models import LLMRuntime
from src.runtimes.schemas import LLMRuntimeSpec

pytestmark = pytest.mark.django_db(transaction=True)


def make_runtime(**kwargs):
    values = {"name": "Runtime", "allowed_models": ["gpt-4o"], "secret_ref": "test-key", **kwargs}
    return LLMRuntime.objects.create(**values)


def test_runtime_profile_defaults_and_snapshot_round_trip(settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://api.openai.com/v1", "http://localhost:11434/v1"]
    runtime = make_runtime(secret_ref="openai-production")
    assert runtime.revision == 1
    assert runtime.capabilities == {
        "tools": False,
        "structured_outputs": False,
        "streaming": False,
        "usage": False,
        "strict_tools": False,
        "token_limit_parameter": "max_completion_tokens",
        "temperature": False,
    }
    snapshot = runtime.to_spec().model_dump(mode="json")
    assert snapshot["secret_ref"] == "openai-production"
    assert "api_key" not in snapshot
    assert LLMRuntimeSpec.model_validate(snapshot) == runtime.to_spec()
    local = make_runtime(profile="ollama", base_url="http://localhost:11434/v1/")
    assert local.base_url == "http://localhost:11434/v1"
    assert local.capabilities["token_limit_parameter"] == "max_tokens"


def test_revision_tracks_config_changes_and_ignores_noop_saves():
    runtime = make_runtime()
    runtime.save()
    assert runtime.revision == 1
    runtime.enabled = False
    runtime.save(update_fields={"enabled"})
    assert runtime.revision == 2
    runtime.refresh_from_db()
    assert runtime.enabled is False
    assert runtime.revision == 2
    runtime.save(update_fields={"enabled"})
    assert runtime.revision == 2
    runtime.secret_ref = "rotated-key-alias"
    runtime.save()
    assert runtime.revision == 3
    runtime.owner = get_user_model().objects.create_user(username="runtime-owner")
    runtime.save(update_fields={"owner"})
    assert runtime.revision == 4


def test_partial_save_does_not_count_unsaved_changes():
    runtime = make_runtime()
    runtime.enabled = False
    runtime.save(update_fields={"name"})
    runtime.refresh_from_db()
    assert runtime.enabled is True
    assert runtime.revision == 1


def test_revision_cannot_be_set_by_callers_or_reset_by_stale_instances():
    runtime = make_runtime(revision=99)
    assert runtime.revision == 1
    stale = LLMRuntime.objects.get(pk=runtime.pk)
    runtime.enabled = False
    runtime.save(update_fields={"enabled"})
    stale.name = "Renamed after concurrent update"
    stale.revision = 99
    stale.save(update_fields={"name", "revision"})
    runtime.refresh_from_db()
    assert runtime.revision == 3
    assert runtime.enabled is False
    assert runtime.name == stale.name


@pytest.mark.parametrize(
    "values",
    [
        {"allowed_models": []},
        {"allowed_models": ["model", "model"]},
        {"allowed_models": [""]},
        {"capabilities": {"unknown": True}},
        {"capabilities": {"tools": "true"}},
        {"capabilities": {"strict_tools": True}},
        {"secret_ref": "Bearer secret value"},
        {"base_url": "https://attacker.example/v1"},
        {"profile": "unrecognized"},
    ],
)
def test_direct_orm_save_enforces_runtime_validation(values):
    values = {"name": "Invalid", "allowed_models": ["model"], "secret_ref": "test-key", **values}
    with pytest.raises(ValidationError):
        LLMRuntime.objects.create(**values)
    assert not LLMRuntime.objects.exists()


def test_invalid_update_preserves_database_revision_and_configuration():
    runtime = make_runtime()
    runtime.allowed_models = []
    with pytest.raises(ValidationError):
        runtime.save()
    runtime.refresh_from_db()
    assert runtime.allowed_models == ["gpt-4o"]
    assert runtime.revision == 1
