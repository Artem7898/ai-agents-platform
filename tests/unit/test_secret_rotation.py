"""Async secret refresh never leases expired clients or interrupts active calls."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

import src.runtimes.clients as clients
import src.runtimes.secrets as secrets
from src.runtimes.schemas import RuntimeCapabilities, RuntimeSpec
from src.runtimes.secret_contracts import FetchedSecret
from src.runtimes.secrets import ResolvedCredential, RuntimeConfigurationError


def runtime(alias="cloud", **changes):
    return RuntimeSpec(
        id=str(uuid.uuid4()),
        name="Cloud",
        profile="openai_compatible",
        base_url="https://cloud.example/v1",
        secret_ref=alias,
        allowed_models=("model",),
        capabilities=RuntimeCapabilities(),
        revision=1,
        **changes,
    )


def credential(value):
    return ResolvedCredential(value=SecretStr(value), _version=value)


@pytest.fixture
def factory(monkeypatch, settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://cloud.example/v1"]
    made = []

    def create(base_url, resolved):
        client = AsyncMock()
        client.test_key = resolved.value.get_secret_value()
        made.append(client)
        return client

    monkeypatch.setattr(clients, "_create_client", create)
    monkeypatch.setattr(clients, "credential_is_current", lambda *args: True)
    return made


async def test_other_runtime_can_lease_while_one_secret_fetch_is_blocked(factory, monkeypatch):
    started, proceed = asyncio.Event(), asyncio.Event()

    async def resolve(profile, alias):
        if alias == "slow":
            started.set()
            await proceed.wait()
        return credential(f"test-{alias}")

    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    pool = clients.RuntimeClientPool()

    async def slow():
        async with pool.lease(runtime("slow")) as client:
            return client

    task = asyncio.create_task(slow())
    await started.wait()
    async with asyncio.timeout(1):
        async with pool.lease(runtime("fast")) as fast:
            assert fast.test_key == "test-fast"
    assert not task.done()
    proceed.set()
    assert (await task).test_key == "test-slow"
    await pool.close()


async def test_old_fetch_finishing_after_rotation_cannot_retire_newer_client(factory, monkeypatch):
    old, new = credential("test-old"), credential("test-new")
    started, proceed = asyncio.Event(), asyncio.Event()
    first = True

    async def resolve(profile, alias):
        nonlocal first
        if first:
            first = False
            started.set()
            await proceed.wait()
            return old
        return new

    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    monkeypatch.setattr(clients, "credential_is_current", lambda value, *args: value is new)
    pool = clients.RuntimeClientPool()
    spec = runtime()

    async def delayed():
        async with pool.lease(spec) as client:
            return client

    task = asyncio.create_task(delayed())
    await started.wait()
    async with pool.lease(spec) as newest:
        proceed.set()
        assert await task is newest
        newest.close.assert_not_awaited()
    assert len(factory) == 1 and newest.test_key == "test-new"
    await pool.close()
    newest.close.assert_awaited_once()


async def test_credential_expiring_before_pool_lock_is_re_resolved(factory, monkeypatch):
    old, new = credential("test-old"), credential("test-new")
    resolved = asyncio.Event()
    values = [old, new]

    async def resolve(*args):
        value = values.pop(0)
        resolved.set()
        return value

    current = old
    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    monkeypatch.setattr(clients, "credential_is_current", lambda value, *args: value is current)
    pool = clients.RuntimeClientPool()
    await pool._lock.acquire()

    async def lease():
        async with pool.lease(runtime()) as client:
            return client

    task = asyncio.create_task(lease())
    await resolved.wait()
    current = new
    pool._lock.release()
    client = await task
    assert client.test_key == "test-new" and len(factory) == 1
    await pool.close()


async def test_failed_refresh_never_falls_back_to_cached_sdk_client(factory, monkeypatch):
    fail = False

    async def resolve(*args):
        if fail:
            raise RuntimeConfigurationError("Secret backend is unavailable")
        return credential("test-original")

    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec) as active:
        fail = True
        with pytest.raises(RuntimeConfigurationError, match="unavailable"):
            async with pool.lease(spec):
                pytest.fail("The cached SDK client bypassed a failed secret refresh")
        active.close.assert_not_awaited()
        assert len(factory) == 1
    await pool.close()


async def test_failed_refresh_closes_idle_cached_client(factory, monkeypatch):
    resolve = AsyncMock(return_value=credential("test-original"))
    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    pool = clients.RuntimeClientPool()
    spec = runtime()
    async with pool.lease(spec) as idle:
        pass
    resolve.side_effect = RuntimeConfigurationError("Secret backend is unavailable")
    with pytest.raises(RuntimeConfigurationError):
        async with pool.lease(spec):
            pytest.fail("A failed refresh reused the idle SDK client")
    idle.close.assert_awaited_once()
    assert not pool._entries
    await pool.close()
    idle.close.assert_awaited_once()


async def test_endpoint_revocation_during_secret_fetch_blocks_client_creation(
    factory, monkeypatch, settings
):
    async def resolve(*args):
        settings.LLM_ALLOWED_BASE_URLS = []
        return credential("test-current")

    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    pool = clients.RuntimeClientPool()
    with pytest.raises(RuntimeConfigurationError, match="endpoint"):
        async with pool.lease(runtime()):
            pytest.fail("An endpoint revoked during secret fetch was admitted")
    assert factory == []
    await pool.close()


async def test_continuously_stale_credentials_fail_closed_with_bounded_fetches(
    factory, monkeypatch
):
    resolve = AsyncMock(return_value=credential("test-stale"))
    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    monkeypatch.setattr(clients, "credential_is_current", lambda *args: False)
    pool = clients.RuntimeClientPool()
    with pytest.raises(RuntimeConfigurationError, match="changed before dispatch"):
        async with asyncio.timeout(1):
            async with pool.lease(runtime()):
                pytest.fail("A stale credential was admitted")
    assert 1 < resolve.await_count <= 10 and factory == []
    await pool.close()


async def test_closed_pool_does_not_contact_secret_manager(factory, monkeypatch):
    resolve = AsyncMock(return_value=credential("test-current"))
    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    pool = clients.RuntimeClientPool()
    await pool.close()
    with pytest.raises(RuntimeConfigurationError, match="closed"):
        async with pool.lease(runtime()):
            pytest.fail("A closed SDK pool was admitted")
    resolve.assert_not_awaited()


async def test_shutdown_closes_secret_resolver_even_without_sdk_pool(monkeypatch):
    clients._pools.pop(asyncio.get_running_loop(), None)
    close = AsyncMock()
    monkeypatch.setattr(clients, "close_runtime_secret_resolvers", close)
    await clients.close_runtime_clients()
    close.assert_awaited_once()


async def test_repeated_cancellation_cannot_interrupt_retired_client_cleanup(factory, monkeypatch):
    old, new = credential("test-old"), credential("test-new")
    current = old

    async def resolve(*args):
        return current

    monkeypatch.setattr(clients, "resolve_runtime_credential_async", resolve)
    active, release = asyncio.Event(), asyncio.Event()
    closing, finish_close, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pool = clients.RuntimeClientPool()
    spec = runtime()

    async def delayed_close():
        closing.set()
        await finish_close.wait()
        closed.set()

    async def original_request():
        async with pool.lease(spec) as client:
            client.close.side_effect = delayed_close
            active.set()
            await release.wait()

    task = asyncio.create_task(original_request())
    await active.wait()
    current = new
    async with pool.lease(spec) as replacement:
        release.set()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not closed.is_set()
        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        factory[0].close.assert_awaited_once()
        replacement.close.assert_not_awaited()
    await pool.close()
    replacement.close.assert_awaited_once()


@pytest.fixture
def external_manager(factory, monkeypatch, settings):
    settings.LLM_SECRET_BINDINGS = {
        "cloud": {"backend": "vault", "path": "approved/key", "field": "api_key"}
    }
    settings.LLM_SECRET_CACHE_TTL_SECONDS = 10
    settings.LLM_SECRET_CACHE_MAX_ENTRIES = 8
    clock = [0.0]
    monkeypatch.setattr(secrets, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(clients, "credential_is_current", secrets.credential_is_current)
    manager = SimpleNamespace(value="test-old", version="1", fail=False, calls=0, close=AsyncMock())

    async def fetch(alias, binding):
        manager.calls += 1
        if manager.fail:
            raise RuntimeConfigurationError("Secret backend is unavailable")
        return FetchedSecret(value=SecretStr(manager.value), version=manager.version)

    manager.fetch = fetch
    monkeypatch.setattr(secrets, "_create_provider", lambda backend: manager)
    return manager, clock


async def test_expired_real_resolver_rotates_sdk_and_preserves_active_lease(
    factory, external_manager
):
    manager, clock = external_manager
    pool = clients.RuntimeClientPool()
    spec = runtime()
    try:
        async with pool.lease(spec) as old:
            manager.value, manager.version = "test-new", "2"
            clock[0] = 11
            async with pool.lease(spec) as replacement:
                assert replacement is not old and replacement.test_key == "test-new"
                assert manager.calls == 2
                old.close.assert_not_awaited()
            old.close.assert_not_awaited()
        old.close.assert_awaited_once()
    finally:
        await pool.close()
        await secrets.close_runtime_secret_resolvers()
    manager.close.assert_awaited_once()


async def test_expired_real_resolver_failure_cannot_use_sdk_or_settings_fallback(
    factory, external_manager, settings
):
    manager, clock = external_manager
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-forbidden-fallback")}
    pool = clients.RuntimeClientPool()
    spec = runtime()
    try:
        async with pool.lease(spec) as original:
            assert original.test_key == "test-old"
        manager.fail = True
        clock[0] = 11
        with pytest.raises(RuntimeConfigurationError, match="could not be resolved"):
            async with pool.lease(spec):
                pytest.fail("An expired key bypassed the external manager failure")
        original.close.assert_awaited_once()
        assert manager.calls == 2 and len(factory) == 1
    finally:
        await pool.close()
        await secrets.close_runtime_secret_resolvers()


async def test_real_resolver_binding_change_during_fetch_never_leases_old_key(
    factory, external_manager, monkeypatch, settings
):
    manager, _ = external_manager
    started, proceed = asyncio.Event(), asyncio.Event()

    async def fetch(alias, binding):
        manager.calls += 1
        if binding["path"] == "approved/key":
            started.set()
            await proceed.wait()
            return FetchedSecret(value=SecretStr("test-old"), version="1")
        return FetchedSecret(value=SecretStr("test-new"), version="2")

    manager.fetch = fetch
    pool = clients.RuntimeClientPool()
    spec = runtime()

    async def delayed():
        async with pool.lease(spec) as client:
            return client

    task = asyncio.create_task(delayed())
    try:
        await started.wait()
        settings.LLM_SECRET_BINDINGS = {
            "cloud": {"backend": "vault", "path": "approved/new-key", "field": "api_key"}
        }
        async with pool.lease(spec) as newest:
            proceed.set()
            assert await task is newest
            assert newest.test_key == "test-new" and len(factory) == 1
            newest.close.assert_not_awaited()
    finally:
        proceed.set()
        await task
        await pool.close()
        await secrets.close_runtime_secret_resolvers()
