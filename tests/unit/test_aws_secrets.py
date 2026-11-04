"""AWS secret reads use a fake SDK and never contact AWS or discover real identity."""

import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import pytest

import src.runtimes.aws_secrets as aws
from src.runtimes.secret_contracts import RuntimeConfigurationError


@pytest.fixture
def backend(monkeypatch):
    clients = []
    responses = [{"SecretString": "test-api-key", "VersionId": "version-1"}]

    def create(region, timeout):
        client = SimpleNamespace(
            calls=[], closed=False, region=region, timeout=timeout, thread=threading.get_ident()
        )

        def get_secret_value(**kwargs):
            client.calls.append(kwargs)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response() if callable(response) else response

        def close():
            client.closed = True

        client.get_secret_value = get_secret_value
        client.close = close
        clients.append(client)
        return client

    monkeypatch.setattr(aws, "_create_client", create)
    return clients, responses


def binding(**changes):
    return {"backend": "aws", "secret_id": "ai-agents/cloud-main", **changes}


async def test_reads_current_version_and_closes_client_off_event_loop(backend):
    clients, _ = backend
    provider = aws.AwsSecretProvider(region="eu-central-1", timeout=2)
    result = await provider.fetch("cloud-main", binding())
    assert result.value.get_secret_value() == "test-api-key"
    assert result.version == "version-1"
    assert "test-api-key" not in repr(result)
    assert "version-1" not in repr(result)
    assert clients[0].calls == [{"SecretId": "ai-agents/cloud-main", "VersionStage": "AWSCURRENT"}]
    assert clients[0].thread != threading.get_ident()
    assert clients[0].region == "eu-central-1" and clients[0].timeout == 2
    assert clients[0].closed
    await provider.close()


async def test_reads_flat_json_field_from_full_arn(backend):
    _, responses = backend
    responses[:] = [
        {
            "SecretString": json.dumps({"api_key": "next-api-key", "ignored": "private-other"}),
            "VersionId": "version-2",
            "VersionStages": ["AWSCURRENT", "custom-stage"],
        }
    ]
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    result = await provider.fetch(
        "cloud",
        binding(
            secret_id="arn:aws:secretsmanager:eu-central-1:123456789012:secret:cloud-main-AbCdEf",
            field="api_key",
        ),
    )
    assert result.value.get_secret_value() == "next-api-key"
    await provider.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "gcp"},
        {"secret_id": ""},
        {"secret_id": "private value"},
        {"secret_id": "x" * 2049},
        {"secret_id": "https://attacker.example/key"},
        {"secret_id": "arn:aws:s3:eu-central-1:123456789012:secret:wrong"},
        {"secret_id": "arn:aws:secretsmanager:eu-central-1:wrong:secret:wrong"},
        {"field": None},
        {"field": ""},
        {"version_id": "pinned-version"},
        {"region": "attacker-region"},
    ],
)
async def test_invalid_bindings_never_initialize_sdk(backend, changes):
    clients, _ = backend
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    with pytest.raises(RuntimeConfigurationError, match="binding is invalid"):
        await provider.fetch("cloud", binding(**changes))
    assert clients == []
    await provider.close()


@pytest.mark.parametrize(
    "response",
    [
        {"SecretBinary": b"private-binary", "VersionId": "v1"},
        {"SecretString": b"private-value", "VersionId": "v1"},
        {"SecretString": "private-value"},
        {"SecretString": "private-value", "VersionId": "private version"},
        {"SecretString": "private-value", "VersionId": "v1", "VersionStages": ["AWSPREVIOUS"]},
        {"SecretString": "private-value", "VersionId": "v1", "VersionStages": []},
        {"SecretString": "private-value\n", "VersionId": "v1"},
        {"SecretString": " private-value", "VersionId": "v1"},
        {"SecretString": "private\rvalue", "VersionId": "v1"},
        {"SecretString": "x" * 16_385, "VersionId": "v1"},
        {"SecretString": "x" * 65_537, "VersionId": "v1"},
        {"SecretString": "\ud800", "VersionId": "v1"},
        None,
    ],
)
async def test_malformed_responses_are_safe_and_close_sdk(backend, response, caplog):
    clients, responses = backend
    responses[:] = [response]
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud", binding())
    assert str(error.value) == "The AWS secret is unavailable"
    assert "private" not in caplog.text
    assert clients[0].closed
    await provider.close()


@pytest.mark.parametrize(
    "payload",
    [
        "private-json",
        "[]",
        "{}",
        '{"api_key":123}',
        '{"api_key":null}',
        '{"api_key":"private-first","api_key":"private-second"}',
        '{"api_key":"valid","other":NaN}',
    ],
)
async def test_json_field_errors_do_not_echo_secret_payload(backend, payload):
    clients, responses = backend
    responses[:] = [{"SecretString": payload, "VersionId": "v1"}]
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud", binding(field="api_key"))
    assert str(error.value) == "The AWS secret is unavailable"
    assert clients[0].closed
    await provider.close()


async def test_service_exception_is_sanitized_and_sdk_is_not_retried(backend):
    clients, responses = backend
    responses[:] = [RuntimeError("denied private-key at private-locator")]
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud", binding())
    assert str(error.value) == "The AWS secret is unavailable"
    assert len(clients) == 1 and len(clients[0].calls) == 1
    assert clients[0].closed
    await provider.close()


async def test_cancelled_waiter_keeps_sdk_cleanup_and_shutdown_waits(backend):
    clients, responses = backend
    started = threading.Event()
    finish = threading.Event()

    def slow_response():
        started.set()
        if not finish.wait(5):
            raise RuntimeError("Test thread timeout")
        return {"SecretString": "test-api-key", "VersionId": "v1"}

    responses[:] = [slow_response]
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    fetch = asyncio.create_task(provider.fetch("cloud", binding()))
    assert await asyncio.to_thread(started.wait, 2)
    fetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fetch
    shutdown = asyncio.create_task(provider.close())
    await asyncio.sleep(0)
    assert not shutdown.done() and not clients[0].closed
    finish.set()
    await asyncio.wait_for(shutdown, 2)
    assert clients[0].closed
    with pytest.raises(RuntimeConfigurationError, match="closed"):
        await provider.fetch("cloud", binding())
    await provider.close()


def test_sdk_factory_uses_default_identity_and_disables_hidden_retries(monkeypatch):
    calls = []

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Session:
        def client(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "client"

    monkeypatch.setitem(sys.modules, "boto3.session", SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=Config))
    assert aws._create_client("eu-central-1", 3) == "client"
    args, kwargs = calls[0]
    assert args == ("secretsmanager",)
    assert set(kwargs) == {"region_name", "config"}
    assert kwargs["region_name"] == "eu-central-1"
    assert kwargs["config"].kwargs == {
        "connect_timeout": 3,
        "read_timeout": 3,
        "retries": {"total_max_attempts": 1, "mode": "standard"},
    }


async def test_missing_optional_sdk_fails_safely_without_identity_discovery(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setitem(sys.modules, "boto3.session", None)
    provider = aws.AwsSecretProvider(region=None, timeout=5)
    with pytest.raises(RuntimeConfigurationError, match="AWS secret is unavailable"):
        await provider.fetch("cloud", binding())
    await provider.close()


def test_settings_are_read_without_sdk_network(backend, settings):
    clients, _ = backend
    settings.LLM_AWS_REGION = "eu-central-1"
    settings.LLM_SECRET_TIMEOUT_SECONDS = 4
    provider = aws.AwsSecretProvider.from_settings()
    assert provider._region == "eu-central-1" and provider._timeout == 4
    assert clients == []


@pytest.mark.parametrize("timeout", [0, -1, 31, True, float("inf"), float("nan"), "5"])
def test_invalid_timeouts_are_rejected(timeout):
    with pytest.raises(RuntimeConfigurationError, match="configuration is invalid"):
        aws.AwsSecretProvider(region=None, timeout=timeout)
