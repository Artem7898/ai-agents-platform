"""Vault reads are scoped, bounded and safe across key and bootstrap rotation."""

import asyncio
import json
import os

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

import src.runtimes.vault as vault
from src.runtimes.secret_contracts import RuntimeConfigurationError

BINDING = {"backend": "vault", "path": "ai-agents/cloud-main", "field": "api_key"}


def payload(value="test-provider-key", version=1, **metadata):
    return {
        "data": {
            "data": {"api_key": value},
            "metadata": {
                "version": version,
                "destroyed": False,
                "deletion_time": "",
                **metadata,
            },
        }
    }


@pytest.fixture
def transport(monkeypatch):
    requests, responses, clients = [], [], []

    async def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response(request)
        return response

    def create(timeout):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout, follow_redirects=False
        )
        clients.append(client)
        return client

    monkeypatch.setattr(vault, "_create_http_client", create)
    return requests, responses, clients


@pytest_asyncio.fixture
async def provider(transport):
    instance = vault.VaultSecretProvider(
        url="https://vault.example/vault/",
        mount="private",
        namespace="team/production",
        token=SecretStr("test-vault-token"),
    )
    yield instance
    await instance.close()


async def test_reads_only_configured_kv2_field_and_observes_latest_version(provider, transport):
    requests, responses, _ = transport
    responses.extend(
        [httpx.Response(200, json=payload()), httpx.Response(200, json=payload("rotated-key", 2))]
    )
    first = await provider.fetch("cloud-main", BINDING)
    second = await provider.fetch("cloud-main", BINDING)
    assert first.value.get_secret_value() == "test-provider-key"
    assert second.value.get_secret_value() == "rotated-key"
    assert (first.version, second.version) == ("1", "2")
    assert len(requests) == 2
    for request in requests:
        assert request.method == "GET"
        assert request.url == "https://vault.example/vault/v1/private/data/ai-agents/cloud-main"
        assert request.headers["X-Vault-Token"] == "test-vault-token"
        assert request.headers["X-Vault-Namespace"] == "team/production"
        assert "version" not in request.url.params
    assert "test-provider-key" not in repr(first)


async def test_token_file_is_read_again_for_every_fetch(tmp_path, transport):
    path = tmp_path / "vault-token"
    path.write_text("first-bootstrap-token\n")
    provider = vault.VaultSecretProvider(url="https://vault.example", token_file=str(path))
    transport[1].extend([httpx.Response(200, json=payload()), httpx.Response(200, json=payload())])
    try:
        await provider.fetch("cloud-main", BINDING)
        replacement = tmp_path / "replacement"
        replacement.write_text("second-bootstrap-token\r\n")
        replacement.replace(path)
        await provider.fetch("cloud-main", BINDING)
        assert [request.headers["X-Vault-Token"] for request in transport[0]] == [
            "first-bootstrap-token",
            "second-bootstrap-token",
        ]
    finally:
        await provider.close()


async def test_from_settings_keeps_bootstrap_values_out_of_repr(settings, transport):
    settings.LLM_VAULT_URL = "https://vault.example"
    settings.LLM_VAULT_MOUNT = "secret"
    settings.LLM_VAULT_NAMESPACE = ""
    settings.LLM_VAULT_TOKEN = SecretStr("private-bootstrap-token")
    settings.LLM_VAULT_TOKEN_FILE = None
    settings.LLM_SECRET_TIMEOUT_SECONDS = 2.0
    provider = vault.VaultSecretProvider.from_settings()
    transport[1].append(httpx.Response(200, json=payload()))
    try:
        await provider.fetch("cloud-main", BINDING)
        assert "private-bootstrap-token" not in repr(provider)
        assert transport[0][0].headers["X-Vault-Token"] == "private-bootstrap-token"
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "address",
    [
        "http://vault.example",
        "https://user:private-token@vault.example",
        "https://vault.example?token=private-token",
        "https://vault.example#private-token",
        "https://vault.example/../private",
        "https://vault.example/%2e%2e/private",
        "https://vault.example/proxy//private",
        "https://vault.example/\\private",
        "https://vault.example:bad",
        "https://vault.example\n",
        "",
    ],
)
def test_unsafe_addresses_are_rejected_without_echoing_configuration(address, transport):
    with pytest.raises(RuntimeConfigurationError) as error:
        vault.VaultSecretProvider(url=address, token=SecretStr("private-bootstrap-token"))
    assert str(error.value) == "Vault secret configuration is invalid"
    assert transport[2] == []


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"token": SecretStr("private"), "token_file": "/private/token"},
        {"token": "private"},
        {"token_file": ""},
        {"token": SecretStr("private"), "mount": "../private"},
        {"token": SecretStr("private"), "mount": "secret/private"},
        {"token": SecretStr("private"), "namespace": "team\r\nprivate"},
        {"token": SecretStr("private"), "timeout": 0},
        {"token": SecretStr("private"), "timeout": True},
    ],
)
def test_invalid_bootstrap_configuration_cannot_construct_a_client(values, transport):
    with pytest.raises(RuntimeConfigurationError, match="configuration"):
        vault.VaultSecretProvider(url="https://vault.example", **values)
    assert transport[2] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "aws"},
        {"path": "../private"},
        {"path": "ai-agents//private"},
        {"path": "ai-agents/%2fprivate"},
        {"path": "/ai-agents/private"},
        {"path": "ai-agents/private?version=2"},
        {"field": "../private"},
        {"token": "private-token"},
    ],
)
async def test_unapproved_binding_shapes_fail_before_http(provider, transport, changes):
    with pytest.raises(RuntimeConfigurationError, match="configuration"):
        await provider.fetch("cloud-main", {**BINDING, **changes})
    assert transport[0] == []


@pytest.mark.parametrize("status", [301, 302, 403, 404, 429, 500])
async def test_errors_and_redirects_never_retry_or_expose_vendor_content(
    provider, transport, status
):
    transport[1].append(
        httpx.Response(
            status,
            text="private-body-secret",
            headers={"Location": "https://attacker.example/private-token"},
        )
    )
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud-main", BINDING)
    assert str(error.value) == "Vault secret is unavailable"
    assert "private" not in repr(error.value)
    assert len(transport[0]) == 1


@pytest.mark.parametrize(
    "body",
    [
        payload(version=True),
        payload(version=0),
        payload(version="1"),
        payload(destroyed=True),
        payload(deletion_time="2026-11-05T00:00:00Z"),
        {"data": {"data": {"api_key": "private-value"}, "metadata": {"version": 1}}},
        {"errors": ["private-error-secret"]},
        payload(value=None),
        payload(value=""),
        payload(value="  "),
        payload(value="private\r\nvalue"),
        payload(value="private\x00value"),
        payload(value="x" * 16_385),
    ],
)
async def test_invalid_or_deleted_secret_responses_fail_safely(provider, transport, body):
    transport[1].append(httpx.Response(200, json=body))
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud-main", BINDING)
    assert str(error.value) == "Vault secret is unavailable"
    assert "private" not in repr(error.value)


async def test_response_body_limit_prevents_using_oversized_envelopes(provider, transport):
    oversized = payload()
    oversized["unrelated"] = "x" * 65_536
    transport[1].append(httpx.Response(200, content=json.dumps(oversized).encode()))
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud-main", BINDING)


async def test_compressed_responses_cannot_bypass_the_body_budget(provider, transport):
    transport[1].append(httpx.Response(200, content=b"", headers={"Content-Encoding": "gzip"}))
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud-main", BINDING)
    assert transport[0][0].headers["Accept-Encoding"] == "identity"


async def test_total_deadline_stops_trickled_response_and_closes_stream(transport):
    class Trickle(httpx.AsyncByteStream):
        closed = False
        yielded = 0

        async def __aiter__(self):
            for byte in json.dumps(payload()).encode():
                await asyncio.sleep(0.025)
                self.yielded += 1
                yield bytes([byte])

        async def aclose(self):
            self.closed = True

    stream = Trickle()
    transport[1].append(httpx.Response(200, stream=stream))
    provider = vault.VaultSecretProvider(
        url="https://vault.example", token=SecretStr("test-token"), timeout=0.1
    )
    try:
        with pytest.raises(RuntimeConfigurationError) as error:
            await asyncio.wait_for(provider.fetch("cloud-main", BINDING), timeout=1)
        assert str(error.value) == "Vault secret is unavailable"
        assert stream.closed
        assert 0 < stream.yielded < len(json.dumps(payload()).encode())
    finally:
        await provider.close()


async def test_total_deadline_also_bounds_bootstrap_token_read(transport, monkeypatch):
    provider = vault.VaultSecretProvider(
        url="https://vault.example", token=SecretStr("test-token"), timeout=0.1
    )

    async def delayed_token():
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_request_token", delayed_token)
    try:
        with pytest.raises(RuntimeConfigurationError, match="unavailable"):
            await asyncio.wait_for(provider.fetch("cloud-main", BINDING), timeout=1)
        assert transport[0] == []
    finally:
        await provider.close()


async def test_transport_exception_text_is_not_exposed(provider, transport):
    transport[1].append(httpx.ReadTimeout("private-bootstrap-token and private-value"))
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud-main", BINDING)
    assert str(error.value) == "Vault secret is unavailable"


@pytest.mark.parametrize("contents", [b"x" * 16_385, b"\xff", b"private-token\nsecond-line"])
async def test_bad_token_files_are_rejected_before_http(tmp_path, transport, contents):
    path = tmp_path / "token"
    path.write_bytes(contents)
    provider = vault.VaultSecretProvider(url="https://vault.example", token_file=str(path))
    try:
        with pytest.raises(RuntimeConfigurationError) as error:
            await provider.fetch("cloud-main", BINDING)
        assert str(error.value) == "Vault secret is unavailable"
        assert transport[0] == []
    finally:
        await provider.close()


async def test_fifo_token_path_fails_without_blocking_a_worker(tmp_path, transport):
    path = tmp_path / "token-pipe"
    os.mkfifo(path)
    provider = vault.VaultSecretProvider(url="https://vault.example", token_file=str(path))
    try:
        with pytest.raises(RuntimeConfigurationError):
            await asyncio.wait_for(provider.fetch("cloud-main", BINDING), timeout=1)
        assert transport[0] == []
    finally:
        await provider.close()


async def test_cancellation_remains_control_flow_and_shutdown_is_idempotent(transport):
    entered = asyncio.Event()

    async def delayed(request):
        entered.set()
        await asyncio.Event().wait()

    provider = vault.VaultSecretProvider(url="https://vault.example", token=SecretStr("token"))
    transport[1].append(delayed)
    task = asyncio.create_task(provider.fetch("cloud-main", BINDING))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.close()
    await provider.close()
    assert transport[2][0].is_closed
    with pytest.raises(RuntimeConfigurationError):
        await provider.fetch("cloud-main", BINDING)
