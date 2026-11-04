"""Runtime credentials and cached SDK lifetimes never require provider traffic."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

import src.runtimes.clients as clients
from src.runtimes.schemas import RuntimeCapabilities, RuntimeSpec
from src.runtimes.secrets import RuntimeConfigurationError, resolve_runtime_credential


def runtime(**changes):
    values = {
        "id": str(uuid.uuid4()),
        "name": "Cloud",
        "profile": "openai_compatible",
        "base_url": "https://cloud.example/v1",
        "secret_ref": "cloud",
        "allowed_models": ("custom-model",),
        "capabilities": RuntimeCapabilities(),
        "revision": 1,
    }
    values.update(changes)
    return RuntimeSpec(**values)


@pytest.fixture
def factory(monkeypatch, settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://cloud.example/v1", "http://localhost:11434/v1"]
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-old-credential")}
    made = []

    def create(base_url, credential):
        client = AsyncMock()
        client.endpoint = base_url
        client.test_credential = credential.value.get_secret_value()
        made.append(client)
        return client

    monkeypatch.setattr(clients, "_create_client", create)
    return made


async def test_reuses_same_runtime_client_and_shutdown_closes_once(factory):
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec) as first:
        async with pool.lease(spec) as second:
            assert first is second
    await pool.close()
    await pool.close()
    assert len(factory) == 1
    factory[0].close.assert_awaited_once()


async def test_secret_rotation_replaces_client_without_interrupting_active_lease(factory, settings):
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec) as old:
        settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-rotated-credential")}
        async with pool.lease(spec) as replacement:
            assert replacement is not old
            assert old.test_credential == "test-old-credential"
            assert replacement.test_credential == "test-rotated-credential"
            old.close.assert_not_awaited()
        old.close.assert_not_awaited()
    old.close.assert_awaited_once()
    replacement.close.assert_not_awaited()
    await pool.close()
    replacement.close.assert_awaited_once()


async def test_revision_change_retires_previous_client(factory):
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec):
        pass
    async with pool.lease(spec.model_copy(update={"revision": 2})):
        factory[0].close.assert_awaited_once()
    assert len(factory) == 2
    await pool.close()


async def test_lru_eviction_and_shutdown_wait_for_active_lease(factory):
    pool = clients.RuntimeClientPool(max_clients=1)
    async with pool.lease(runtime()) as first:
        async with pool.lease(runtime()) as second:
            first.close.assert_not_awaited()
            await pool.close()
            second.close.assert_not_awaited()
        second.close.assert_awaited_once()
    first.close.assert_awaited_once()
    assert not pool._entries


async def test_cached_endpoint_revocation_is_enforced_before_reuse(factory, settings):
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec):
        pass
    settings.LLM_ALLOWED_BASE_URLS = []
    with pytest.raises(RuntimeConfigurationError, match="endpoint"):
        async with pool.lease(spec):
            pytest.fail("A revoked endpoint was reused")
    assert len(factory) == 1
    await pool.close()


@pytest.mark.parametrize("secret_ref", [None, "missing", "OPENAI_API_KEY"])
async def test_cloud_has_no_key_or_environment_fallback(factory, settings, monkeypatch, secret_ref):
    settings.LLM_RUNTIME_SECRETS = {}
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    pool = clients.RuntimeClientPool()
    expected_error = ValidationError if secret_ref is None else RuntimeConfigurationError
    with pytest.raises(expected_error):
        async with pool.lease(runtime(secret_ref=secret_ref)):
            pytest.fail("Unconfigured credential was accepted")
    assert not factory
    await pool.close()


async def test_ollama_dummy_key_only_applies_when_no_alias_is_configured(factory, settings):
    spec = runtime(profile="ollama", base_url="http://localhost:11434/v1", secret_ref=None)
    pool = clients.RuntimeClientPool()
    async with pool.lease(spec) as client:
        assert client.test_credential == "ollama-local-no-auth"
    with pytest.raises(RuntimeConfigurationError):
        async with pool.lease(spec.model_copy(update={"secret_ref": "missing"})):
            pytest.fail("Missing alias fell back to unauthenticated Ollama")
    assert len(factory) == 1
    await pool.close()


def test_resolved_credentials_do_not_expose_key_or_version_in_repr(settings):
    key = "private-test-value"
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr(key)}
    credential = resolve_runtime_credential("openai", "cloud")
    assert key not in repr(credential)
    assert credential._version not in repr(credential)
    assert key not in repr(credential.value)


def test_registry_is_scoped_to_event_loop_and_shutdown_removes_pool(factory):
    spec = runtime()

    async def use_on_loop():
        async with clients.runtime_client(spec) as client:
            async with clients.runtime_client(spec) as again:
                assert client is again
        await clients.close_runtime_clients()
        return client

    first = asyncio.run(use_on_loop())
    second = asyncio.run(use_on_loop())
    assert first is not second
    assert len(factory) == 2
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


async def test_failed_client_replacement_preserves_existing_cached_client(
    factory, settings, monkeypatch
):
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec) as original:
        pass
    successful_factory = clients._create_client

    def broken_factory(*args):
        raise RuntimeError("Client initialization failed")

    monkeypatch.setattr(clients, "_create_client", broken_factory)
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-rotated-credential")}
    with pytest.raises(RuntimeConfigurationError, match="initialization"):
        async with pool.lease(spec):
            pytest.fail("Broken replacement was accepted")
    original.close.assert_not_awaited()
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-old-credential")}
    monkeypatch.setattr(clients, "_create_client", successful_factory)
    async with pool.lease(spec) as reused:
        assert reused is original
    await pool.close()
    original.close.assert_awaited_once()
