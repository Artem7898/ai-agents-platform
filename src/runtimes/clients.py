"""Loop-local SDK clients with lease-safe configuration and credential rotation."""

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

import httpx
from django.core.exceptions import ValidationError
from openai import AsyncOpenAI

from .policies import validate_runtime_endpoint
from .schemas import RuntimeSpec
from .secrets import (
    ResolvedCredential,
    RuntimeConfigurationError,
    close_runtime_secret_resolvers,
    credential_is_current,
    resolve_runtime_credential_async,
)


@dataclass(frozen=True, repr=False)
class _ClientKey:
    runtime_id: str
    revision: int
    configuration: str
    credential_version: str = field(repr=False)


@dataclass(repr=False)
class _ClientEntry:
    client: AsyncOpenAI = field(repr=False)
    leases: int = 0
    retired: bool = False


def _create_client(base_url: str, credential: ResolvedCredential) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=credential.value.get_secret_value(),
        base_url=base_url,
        timeout=60.0,
        max_retries=2,
        http_client=httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            timeout=60.0,
            follow_redirects=False,
        ),
    )


async def _close_clients(entries: list[_ClientEntry]) -> None:
    for entry in entries:
        # Cleanup failures must not leak a vendor exception or block a replacement.
        with suppress(Exception):
            await entry.client.close()


async def _shield_cleanup(operation: Awaitable[None]) -> None:
    """Finish lease bookkeeping even if shutdown receives repeated cancellation."""
    task = asyncio.ensure_future(operation)
    cancelled = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled


class RuntimeClientPool:
    """Bounded LRU cache; eviction/rotation closes clients after their last lease."""

    def __init__(self, max_clients: int = 32) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        self._max_clients = max_clients
        self._entries: OrderedDict[_ClientKey, _ClientEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    @asynccontextmanager
    async def lease(self, runtime: RuntimeSpec) -> AsyncIterator[AsyncOpenAI]:
        if self._closed:
            raise RuntimeConfigurationError("Runtime client pool is closed")
        if not runtime.enabled:
            raise RuntimeConfigurationError("Runtime is disabled")
        # Revalidate even for cached clients so a revoked allowlist applies immediately.
        try:
            base_url = validate_runtime_endpoint(runtime.base_url, runtime.profile)
        except ValidationError:
            raise RuntimeConfigurationError("Runtime endpoint is not permitted") from None
        for _ in range(8):
            # Credential I/O stays outside the pool lock. Each returned value
            # must still be current when it actually acquires a client lease.
            try:
                credential = await resolve_runtime_credential_async(
                    runtime.profile, runtime.secret_ref
                )
            except RuntimeConfigurationError:
                # A denied or expired refresh cannot leave an idle SDK client
                # retaining a key that this runtime can no longer acquire.
                await _shield_cleanup(self._retire_runtime(str(runtime.id)))
                raise
            key = _ClientKey(
                runtime_id=str(runtime.id),
                revision=runtime.revision,
                configuration=hashlib.sha256(runtime.model_dump_json().encode()).hexdigest(),
                credential_version=credential._version,
            )
            to_close = []
            async with self._lock:
                if self._closed:
                    raise RuntimeConfigurationError("Runtime client pool is closed")
                if not credential_is_current(credential, runtime.profile, runtime.secret_ref):
                    continue
                # An allowlist may be revoked while the secret manager is being
                # contacted; check it again before touching a cached SDK client.
                try:
                    base_url = validate_runtime_endpoint(runtime.base_url, runtime.profile)
                except ValidationError:
                    raise RuntimeConfigurationError("Runtime endpoint is not permitted") from None
                entry = self._entries.get(key)
                if entry is None:
                    # Construct first: a failed replacement must leave existing
                    # cached clients intact, but can never bypass secret lookup.
                    try:
                        entry = _ClientEntry(_create_client(base_url, credential))
                    except Exception:
                        raise RuntimeConfigurationError(
                            "Runtime client initialization failed"
                        ) from None
                    obsolete = [k for k in self._entries if k.runtime_id == key.runtime_id]
                    for old_key in obsolete:
                        old = self._entries.pop(old_key)
                        old.retired = True
                        if old.leases == 0:
                            to_close.append(old)
                    while len(self._entries) >= self._max_clients:
                        _, old = self._entries.popitem(last=False)
                        old.retired = True
                        if old.leases == 0:
                            to_close.append(old)
                    self._entries[key] = entry
                else:
                    self._entries.move_to_end(key)
                entry.leases += 1
                break
        else:
            # Continuous changes must fail closed instead of spinning or
            # leasing whichever key happened to finish resolving last.
            raise RuntimeConfigurationError("Runtime credential changed before dispatch")
        try:
            await _shield_cleanup(_close_clients(to_close))
            yield entry.client
        finally:
            await _shield_cleanup(self._release(entry))

    async def _release(self, entry: _ClientEntry) -> None:
        async with self._lock:
            entry.leases -= 1
            close_entry = entry.retired and entry.leases == 0
        if close_entry:
            await _close_clients([entry])

    async def _retire_runtime(self, runtime_id: str) -> None:
        to_close = []
        async with self._lock:
            obsolete = [key for key in self._entries if key.runtime_id == runtime_id]
            for key in obsolete:
                entry = self._entries.pop(key)
                entry.retired = True
                if entry.leases == 0:
                    to_close.append(entry)
        await _close_clients(to_close)

    async def close(self) -> None:
        await _shield_cleanup(self._close())

    async def _close(self) -> None:
        async with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.retired = True
        await _close_clients([entry for entry in entries if entry.leases == 0])


_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, RuntimeClientPool] = WeakKeyDictionary()


@asynccontextmanager
async def runtime_client(runtime: RuntimeSpec) -> AsyncIterator[AsyncOpenAI]:
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        pool = RuntimeClientPool()
        _pools[loop] = pool
    async with pool.lease(runtime) as client:
        yield client


async def close_runtime_clients() -> None:
    """Close the current ASGI/worker loop's clients; active leases retire safely."""
    pool = _pools.pop(asyncio.get_running_loop(), None)

    async def close_all() -> None:
        try:
            if pool is not None:
                await pool.close()
        finally:
            await close_runtime_secret_resolvers()

    await _shield_cleanup(close_all())
