"""Secret refresh concurrency and expiry are independent of vendor services."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import SecretStr

import src.runtimes.secrets as secrets
from src.runtimes.secret_contracts import FetchedSecret, validate_secret_value
from src.runtimes.secrets import (
    RuntimeConfigurationError,
    close_runtime_secret_resolvers,
    credential_is_current,
    resolve_runtime_credential,
    resolve_runtime_credential_async,
)


class FakeProvider:
    def __init__(self, backend):
        self.backend = backend
        self.calls = []
        self.closed = 0
        self.value = FetchedSecret(SecretStr(f"test-{backend}-key"), "1")
        self.handler = None

    async def fetch(self, alias, binding):
        self.calls.append((alias, copy.deepcopy(binding)))
        if self.handler is not None:
            return await self.handler(alias, binding)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    async def close(self):
        self.closed += 1


@pytest.fixture
def configured(settings, monkeypatch):
    settings.LLM_SECRET_BINDINGS = {
        "cloud": {"backend": "vault", "path": "private-locator", "field": "api_key"}
    }
    settings.LLM_RUNTIME_SECRETS = {
        "local": SecretStr("test-local-key"),
        "cloud": SecretStr("local-must-not-be-used"),
    }
    settings.LLM_SECRET_CACHE_TTL_SECONDS = 30
    settings.LLM_SECRET_CACHE_MAX_ENTRIES = 128
    clock = [100.0]
    monkeypatch.setattr(secrets, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    providers = []

    def create(backend):
        provider = FakeProvider(backend)
        providers.append(provider)
        return provider

    monkeypatch.setattr(secrets, "_create_provider", create)
    return settings, clock, providers


@pytest_asyncio.fixture(autouse=True)
async def close_resolvers():
    await close_runtime_secret_resolvers()
    yield
    await close_runtime_secret_resolvers()


async def test_local_aliases_are_immediate_and_external_binding_never_falls_back(configured):
    settings, _, providers = configured
    local = resolve_runtime_credential("openai", "local")
    assert local.value.get_secret_value() == "test-local-key"
    settings.LLM_RUNTIME_SECRETS["local"] = SecretStr("test-new-local-key")
    assert not credential_is_current(local, "openai", "local")
    assert (await resolve_runtime_credential_async("openai", "local")).value.get_secret_value() == (
        "test-new-local-key"
    )
    with pytest.raises(RuntimeConfigurationError, match="asynchronous"):
        resolve_runtime_credential("openai", "cloud")
    external = await resolve_runtime_credential_async("openai", "cloud")
    assert external.value.get_secret_value() == "test-vault-key"
    assert len(providers) == 1


async def test_three_backends_can_resolve_simultaneously(configured):
    settings, _, providers = configured
    settings.LLM_SECRET_BINDINGS = {
        backend: {"backend": backend, "path": "server-configured"}
        for backend in ("vault", "aws", "gcp")
    }
    results = await asyncio.gather(
        *(
            resolve_runtime_credential_async("openai", backend)
            for backend in settings.LLM_SECRET_BINDINGS
        )
    )
    assert {result.value.get_secret_value() for result in results} == {
        "test-vault-key",
        "test-aws-key",
        "test-gcp-key",
    }
    assert {provider.backend for provider in providers} == {"vault", "aws", "gcp"}


async def test_positive_ttl_cache_and_rotation_fence_old_credentials(configured):
    _, clock, providers = configured
    old = await resolve_runtime_credential_async("openai", "cloud")
    assert await resolve_runtime_credential_async("openai", "cloud") is old
    assert len(providers[0].calls) == 1
    clock[0] += 31
    assert not credential_is_current(old, "openai", "cloud")
    providers[0].value = FetchedSecret(SecretStr("test-rotated-key"), "2")
    new = await resolve_runtime_credential_async("openai", "cloud")
    assert new.value.get_secret_value() == "test-rotated-key"
    assert not credential_is_current(old, "openai", "cloud")
    assert credential_is_current(new, "openai", "cloud")


async def test_expired_failure_evicts_secret_and_cannot_use_local_fallback(configured):
    _, clock, providers = configured
    old = await resolve_runtime_credential_async("openai", "cloud")
    clock[0] += 31
    providers[0].value = RuntimeError("private-value-and-backend-response")
    with pytest.raises(RuntimeConfigurationError) as error:
        await resolve_runtime_credential_async("openai", "cloud")
    assert "private-value" not in str(error.value)
    assert not credential_is_current(old, "openai", "cloud")
    resolver = secrets._resolvers[asyncio.get_running_loop()]
    assert "cloud" not in resolver._cache
    providers[0].value = FetchedSecret(SecretStr("test-recovered-key"), "2")
    recovered = await resolve_runtime_credential_async("openai", "cloud")
    assert recovered.value.get_secret_value() == "test-recovered-key"
    assert len(providers[0].calls) == 3


async def test_zero_ttl_reads_every_time_without_invalidating_same_material(configured):
    settings, _, providers = configured
    settings.LLM_SECRET_CACHE_TTL_SECONDS = 0
    old = await resolve_runtime_credential_async("openai", "cloud")
    same = await resolve_runtime_credential_async("openai", "cloud")
    assert len(providers[0].calls) == 2
    assert credential_is_current(old, "openai", "cloud")
    assert old._generation == same._generation
    providers[0].value = FetchedSecret(SecretStr("test-new-key"), "2")
    await resolve_runtime_credential_async("openai", "cloud")
    assert not credential_is_current(old, "openai", "cloud")


async def test_one_cancelled_waiter_does_not_cancel_shared_refresh(configured):
    _, _, providers = configured
    entered, release = asyncio.Event(), asyncio.Event()
    provider = FakeProvider("vault")

    async def delayed(alias, binding):
        entered.set()
        await release.wait()
        return provider.value

    provider.handler = delayed
    providers.append(provider)
    original = secrets._create_provider
    secrets._create_provider = lambda backend: provider
    try:
        waiters = [
            asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
            for _ in range(8)
        ]
        await entered.wait()
        waiters[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiters[0]
        release.set()
        results = await asyncio.gather(*waiters[1:])
        assert len(provider.calls) == 1
        assert all(result.value.get_secret_value() == "test-vault-key" for result in results)
    finally:
        secrets._create_provider = original


async def test_old_binding_result_finishing_late_cannot_overwrite_new_epoch(configured):
    settings, _, providers = configured
    entered, release = asyncio.Event(), asyncio.Event()
    provider = FakeProvider("vault")

    async def delayed(alias, binding):
        if binding["path"] == "private-locator":
            entered.set()
            await release.wait()
            return FetchedSecret(SecretStr("test-old-key"), "1")
        return FetchedSecret(SecretStr("test-new-key"), "2")

    provider.handler = delayed
    providers.append(provider)
    original = secrets._create_provider
    secrets._create_provider = lambda backend: provider
    try:
        old_task = asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
        await entered.wait()
        settings.LLM_SECRET_BINDINGS["cloud"]["path"] = "replacement-locator"
        new = await resolve_runtime_credential_async("openai", "cloud")
        release.set()
        old_waiter_result = await old_task
        assert new.value.get_secret_value() == "test-new-key"
        assert old_waiter_result._generation == new._generation
        assert credential_is_current(new, "openai", "cloud")
    finally:
        secrets._create_provider = original


async def test_failed_old_epoch_does_not_remove_new_success(configured):
    settings, _, _ = configured
    entered, release = asyncio.Event(), asyncio.Event()
    provider = FakeProvider("vault")

    async def delayed(alias, binding):
        if binding["path"] == "private-locator":
            entered.set()
            await release.wait()
            raise RuntimeError("private-backend-error")
        return FetchedSecret(SecretStr("test-new-key"), "2")

    provider.handler = delayed
    original = secrets._create_provider
    secrets._create_provider = lambda backend: provider
    try:
        old_task = asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
        await entered.wait()
        settings.LLM_SECRET_BINDINGS["cloud"]["path"] = "replacement-locator"
        new = await resolve_runtime_credential_async("openai", "cloud")
        release.set()
        with pytest.raises(RuntimeConfigurationError):
            await old_task
        assert await resolve_runtime_credential_async("openai", "cloud") is new
    finally:
        secrets._create_provider = original


async def test_connection_config_change_retires_provider_after_its_fetch(configured):
    settings, _, providers = configured
    entered, release = asyncio.Event(), asyncio.Event()
    old_provider = FakeProvider("vault")

    async def delayed(alias, binding):
        entered.set()
        await release.wait()
        return old_provider.value

    old_provider.handler = delayed
    new_provider = FakeProvider("vault")
    new_provider.value = FetchedSecret(SecretStr("test-connection-new-key"), "2")
    queue = iter((old_provider, new_provider))
    original = secrets._create_provider
    secrets._create_provider = lambda backend: next(queue)
    try:
        old_task = asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
        await entered.wait()
        settings.LLM_VAULT_TOKEN = SecretStr("new-bootstrap-token")
        new = await resolve_runtime_credential_async("openai", "cloud")
        assert old_provider.closed == 0
        release.set()
        assert (await old_task)._generation == new._generation
        assert old_provider.closed == 1
        assert new_provider.closed == 0
    finally:
        secrets._create_provider = original


async def test_lru_cache_is_bounded_and_evicted_credentials_fail_probe(configured):
    settings, _, providers = configured
    settings.LLM_SECRET_CACHE_MAX_ENTRIES = 1
    settings.LLM_SECRET_BINDINGS["other"] = {
        "backend": "vault",
        "path": "other",
        "field": "api_key",
    }
    old = await resolve_runtime_credential_async("openai", "cloud")
    other = await resolve_runtime_credential_async("openai", "other")
    assert not credential_is_current(old, "openai", "cloud")
    assert credential_is_current(other, "openai", "other")
    resolver = secrets._resolvers[asyncio.get_running_loop()]
    assert len(resolver._cache) == len(resolver._materials) == 1
    await resolve_runtime_credential_async("openai", "cloud")
    assert len(providers[0].calls) == 3


async def test_shutdown_cancels_refresh_and_closes_provider(configured):
    _, _, providers = configured
    entered = asyncio.Event()
    provider = FakeProvider("vault")

    async def never(alias, binding):
        entered.set()
        await asyncio.Event().wait()

    provider.handler = never
    original = secrets._create_provider
    secrets._create_provider = lambda backend: provider
    try:
        waiter = asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
        await entered.wait()
        await close_runtime_secret_resolvers()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert provider.closed == 1
        assert asyncio.get_running_loop() not in secrets._resolvers
    finally:
        secrets._create_provider = original


async def test_audit_metadata_never_contains_key_locator_hash_or_backend_error(
    configured, monkeypatch
):
    _, clock, providers = configured
    records = []
    logger = SimpleNamespace(
        info=lambda event, **fields: records.append((event, fields)),
        warning=lambda event, **fields: records.append((event, fields)),
    )
    monkeypatch.setattr(secrets, "log", logger)
    credential = await resolve_runtime_credential_async("openai", "cloud")
    clock[0] += 31
    providers[0].value = RuntimeError("private-backend-error")
    with pytest.raises(RuntimeConfigurationError):
        await resolve_runtime_credential_async("openai", "cloud")
    encoded = repr(records)
    for hidden in (
        "test-vault-key",
        "private-locator",
        "private-backend-error",
        credential._version,
    ):
        assert hidden not in encoded
    assert [name for name, _ in records] == ["llm_secret_refreshed", "llm_secret_refresh_failed"]
    assert all(set(fields) == {"backend", "alias", "outcome"} for _, fields in records)


@pytest.mark.parametrize("alias", ["missing", "OPENAI_API_KEY", "../../private", ""])
async def test_missing_and_arbitrary_aliases_have_no_environment_fallback(
    configured, monkeypatch, alias
):
    monkeypatch.setenv("OPENAI_API_KEY", "private-env-key")
    with pytest.raises(RuntimeConfigurationError):
        await resolve_runtime_credential_async("openai", alias)


@pytest.mark.parametrize("binding", [None, {}, {"backend": []}, {"backend": "unknown"}])
async def test_invalid_explicit_binding_does_not_fall_back_to_settings(configured, binding):
    settings, _, providers = configured
    settings.LLM_SECRET_BINDINGS["cloud"] = binding
    with pytest.raises(RuntimeConfigurationError):
        await resolve_runtime_credential_async("openai", "cloud")
    assert providers == []


@pytest.mark.parametrize("ttl", [-1, 301, float("nan"), True])
async def test_cache_configuration_is_bounded_before_provider_access(configured, ttl):
    settings, _, providers = configured
    settings.LLM_SECRET_CACHE_TTL_SECONDS = ttl
    with pytest.raises(RuntimeConfigurationError):
        await resolve_runtime_credential_async("openai", "cloud")
    assert providers == []


async def test_credential_reprs_hide_value_version_and_scope(configured):
    credential = await resolve_runtime_credential_async("openai", "cloud")
    assert credential.value.get_secret_value() not in repr(credential)
    assert credential._version not in repr(credential)
    assert credential._scope not in repr(credential)


async def test_closing_and_recreating_resolver_cannot_revalidate_an_old_key(configured):
    _, _, providers = configured
    old = await resolve_runtime_credential_async("openai", "cloud")
    await close_runtime_secret_resolvers()
    assert not credential_is_current(old, "openai", "cloud")
    original = secrets._create_provider

    def create_rotated(backend):
        provider = original(backend)
        provider.value = FetchedSecret(SecretStr("test-new-after-close-key"), "2")
        return provider

    secrets._create_provider = create_rotated
    try:
        new = await resolve_runtime_credential_async("openai", "cloud")
        assert credential_is_current(new, "openai", "cloud")
        assert not credential_is_current(old, "openai", "cloud")
    finally:
        secrets._create_provider = original


async def test_timeout_preserves_singleflight_until_physical_fetch_finishes(configured):
    settings, _, _ = configured
    settings.LLM_SECRET_TIMEOUT_SECONDS = 0.02
    entered, release = asyncio.Event(), asyncio.Event()
    provider = FakeProvider("vault")

    async def delayed(alias, binding):
        entered.set()
        await release.wait()
        return provider.value

    provider.handler = delayed
    original = secrets._create_provider
    secrets._create_provider = lambda backend: provider
    try:
        first = asyncio.create_task(resolve_runtime_credential_async("openai", "cloud"))
        await entered.wait()
        with pytest.raises(RuntimeConfigurationError, match="timed out"):
            await first
        resolver = secrets._resolvers[asyncio.get_running_loop()]
        assert len(resolver._inflight) == 1
        with pytest.raises(RuntimeConfigurationError, match="timed out"):
            await resolve_runtime_credential_async("openai", "cloud")
        assert len(provider.calls) == 1
        running = list(resolver._inflight.values())
        release.set()
        await asyncio.gather(*running, return_exceptions=True)
        assert resolver._inflight == {}
        assert resolver._cache == {}  # A response past its total deadline is not cached.
    finally:
        secrets._create_provider = original


@pytest.mark.parametrize("value", ["private-é", "private\nkey", "private\u2028key"])
def test_llm_header_credentials_are_printable_ascii_without_echoing_input(value):
    with pytest.raises(RuntimeConfigurationError) as error:
        validate_secret_value(value)
    assert "private" not in str(error.value)
