"""Google Secret Manager's SDK is replaced; tests never discover cloud credentials."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from src.runtimes import gcp_secrets
from src.runtimes.secret_contracts import RuntimeConfigurationError

_PROJECT = "123456789012"
_RESOURCE = f"projects/{_PROJECT}/secrets/cloud-key"
_CRC = 0xE3069283


def response(data=b"123456789", *, name=None, crc=_CRC):
    return SimpleNamespace(
        name=name or _RESOURCE + "/versions/9",
        payload=SimpleNamespace(data=data, data_crc32c=crc),
    )


@pytest.fixture
def sdk(monkeypatch):
    calls, threads, factories, closed, checksums = [], [], [], [], []
    responses = [response()]

    class Client:
        def access_secret_version(self, **kwargs):
            calls.append(kwargs)
            threads.append(threading.get_ident())
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            closed.append(threading.get_ident())

    client = Client()

    def create():
        factories.append(threading.get_ident())
        return client

    def checksum(data):
        checksums.append(data)
        return _CRC

    monkeypatch.setattr(gcp_secrets, "_create_client", create)
    monkeypatch.setattr(gcp_secrets, "_checksum", checksum)
    return SimpleNamespace(
        client=client,
        calls=calls,
        responses=responses,
        threads=threads,
        factories=factories,
        closed=closed,
        checksums=checksums,
    )


def binding(**changes):
    return {"backend": "gcp", "secret_id": "cloud-key", **changes}


async def test_uses_latest_exact_resource_adc_sdk_reuse_and_no_hidden_retries(sdk):
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT, timeout=4)
    thread = threading.get_ident()
    result = await provider.fetch("cloud", binding())
    sdk.responses.append(response())
    again = await provider.fetch("cloud", binding())
    assert result.value.get_secret_value() == again.value.get_secret_value() == "123456789"
    assert result.version == "9"
    assert result.value.get_secret_value() not in repr(result)
    assert (
        sdk.calls
        == [{"request": {"name": _RESOURCE + "/versions/latest"}, "retry": None, "timeout": 4.0}]
        * 2
    )
    assert len(sdk.factories) == 1
    assert all(identity != thread for identity in sdk.factories + sdk.threads)
    assert sdk.checksums == [b"123456789", b"123456789"]
    await provider.close()
    await provider.close()
    assert len(sdk.closed) == 1 and sdk.closed[0] != thread


async def test_extracts_optional_string_json_field_after_integrity_check(sdk):
    sdk.responses[:] = [response(json.dumps({"api_key": "private-key", "other": 2}).encode())]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    result = await provider.fetch("cloud", binding(field="api_key"))
    assert result.value.get_secret_value() == "private-key"
    assert len(sdk.checksums) == 1
    await provider.close()


async def test_binding_project_number_can_override_configured_default(sdk):
    provider = gcp_secrets.GcpSecretProvider()
    result = await provider.fetch("cloud", binding(project=_PROJECT))
    assert result.version == "9"
    await provider.close()


def test_from_settings_does_not_create_sdk_or_read_adc(sdk, settings):
    settings.LLM_GCP_PROJECT = _PROJECT
    settings.LLM_SECRET_TIMEOUT_SECONDS = 7
    provider = gcp_secrets.GcpSecretProvider.from_settings()
    assert provider._project == _PROJECT and provider._timeout == 7
    assert sdk.factories == []


@pytest.mark.parametrize("project", ["named-project", "0", "../123", "12/34", 123, True])
def test_project_requires_an_exact_numeric_project_number(project):
    with pytest.raises(RuntimeConfigurationError, match="numeric project number"):
        gcp_secrets.GcpSecretProvider(project=project)


@pytest.mark.parametrize("timeout", [0, 0.01, -1, 31, float("nan"), float("inf"), True, "5"])
def test_timeout_is_bounded_and_finite(timeout):
    with pytest.raises(RuntimeConfigurationError, match="timeout"):
        gcp_secrets.GcpSecretProvider(timeout=timeout)


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "aws"},
        {"project": "named-project"},
        {"project": "0"},
        {"secret_id": "a/b"},
        {"secret_id": "../private"},
        {"secret_id": ""},
        {"field": "../key"},
        {"version": "1"},
        {"credentials": "private"},
        {"secret_id": None},
    ],
)
async def test_invalid_binding_is_rejected_before_sdk_creation(sdk, changes):
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError):
        await provider.fetch("cloud", binding(**changes))
    assert sdk.factories == sdk.calls == []
    await provider.close()


@pytest.mark.parametrize("alias", ["../private", "", "alias/key", "a" * 129])
async def test_alias_is_bounded_server_identifier(sdk, alias):
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError, match="alias"):
        await provider.fetch(alias, binding())
    assert sdk.factories == []
    await provider.close()


@pytest.mark.parametrize("crc", [None, True, -1, 2**32, "123", _CRC + 1])
async def test_integrity_failure_never_returns_a_secret(sdk, crc):
    sdk.responses[:] = [response(crc=crc)]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud", binding())
    await provider.close()


@pytest.mark.parametrize(
    "name",
    [
        "projects/999999999999/secrets/cloud-key/versions/9",
        f"projects/{_PROJECT}/secrets/other-key/versions/9",
        _RESOURCE + "/versions/latest",
        _RESOURCE + "/versions/0",
        _RESOURCE + "/versions/9/private",
        _RESOURCE + "/versions/../9",
    ],
)
async def test_response_must_match_project_secret_and_concrete_version(sdk, name):
    sdk.responses[:] = [response(name=name)]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud", binding())
    await provider.close()


@pytest.mark.parametrize(
    "data",
    [
        "123456789",
        b"",
        b"x" * 65_537,
        b"x" * 16_385,
        b" secret",
        b"secret ",
        b"secret\nsecond",
        b"secret\rsecond",
        b"secret\x00second",
        b"secret\x7fsecond",
        b"\xff",
        "secret\u2028second".encode(),
    ],
)
async def test_bad_payload_is_rejected_without_exposing_its_contents(sdk, data, caplog):
    sdk.responses[:] = [response(data)]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud", binding())
    assert str(error.value) == "Google Secret Manager secret is unavailable"
    assert caplog.text == ""
    await provider.close()


@pytest.mark.parametrize("data", [b"not-json", b"[]", b"{}", b'{"api_key":1}', b'{"api_key":""}'])
async def test_selected_json_field_must_be_a_nonempty_string(sdk, data):
    sdk.responses[:] = [response(data)]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud", binding(field="api_key"))
    await provider.close()


async def test_vendor_errors_do_not_echo_credentials_resources_or_body(sdk, caplog):
    marker = "private-key-from-vendor-error"
    sdk.responses[:] = [RuntimeError(marker + " " + _RESOURCE)]
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError) as error:
        await provider.fetch("cloud", binding())
    assert marker not in str(error.value) and _RESOURCE not in str(error.value)
    assert marker not in caplog.text and _RESOURCE not in caplog.text
    await provider.close()


def test_missing_sdk_has_a_safe_actionable_error(monkeypatch):
    def missing(name):
        raise ImportError("private-import-detail")

    monkeypatch.setattr(gcp_secrets.importlib, "import_module", missing)
    with pytest.raises(RuntimeConfigurationError, match="gcp optional dependency") as error:
        gcp_secrets._create_client()
    assert "private-import-detail" not in str(error.value)


async def test_cancelled_waiter_cannot_close_client_during_thread_operation(sdk, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    access = sdk.client.access_secret_version

    def delayed(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return access(**kwargs)

    monkeypatch.setattr(sdk.client, "access_secret_version", delayed)
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    task = asyncio.create_task(provider.fetch("cloud", binding()))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(provider.close())
    try:
        await asyncio.sleep(0)
        assert sdk.closed == []
        with pytest.raises(RuntimeConfigurationError, match="closed"):
            await provider.fetch("cloud", binding())
    finally:
        release.set()
        await closing
    assert len(sdk.closed) == 1


async def test_shutdown_handles_sdk_transport_close_without_public_close(sdk, monkeypatch):
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    await provider.fetch("cloud", binding())
    monkeypatch.setattr(sdk.client, "close", None)
    sdk.client.transport = SimpleNamespace(close=lambda: sdk.closed.append("transport"))
    await provider.close()
    assert sdk.closed == ["transport"]


def test_optional_real_crc32c_matches_published_test_vector(monkeypatch):
    pytest.importorskip("google_crc32c")
    assert gcp_secrets._checksum(b"123456789") == _CRC


async def test_optional_real_sdk_payload_crc_and_resource_shape(monkeypatch):
    sdk = pytest.importorskip("google.cloud.secretmanager_v1")
    pytest.importorskip("google_crc32c")
    data = b"123456789"
    result = sdk.AccessSecretVersionResponse(
        name=_RESOURCE + "/versions/9", payload={"data": data, "data_crc32c": _CRC}
    )
    client = SimpleNamespace(access_secret_version=lambda **kwargs: result, close=lambda: None)
    monkeypatch.setattr(gcp_secrets, "_create_client", lambda: client)
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    value = await provider.fetch("cloud", binding())
    assert value.value.get_secret_value() == data.decode()
    await provider.close()


async def test_optional_proto_default_zero_cannot_hide_an_absent_checksum(monkeypatch):
    sdk = pytest.importorskip("google.cloud.secretmanager_v1")
    result = sdk.AccessSecretVersionResponse(
        name=_RESOURCE + "/versions/9", payload={"data": b"key-with-zero-crc"}
    )
    client = SimpleNamespace(access_secret_version=lambda **kwargs: result, close=lambda: None)
    monkeypatch.setattr(gcp_secrets, "_create_client", lambda: client)
    monkeypatch.setattr(gcp_secrets, "_checksum", lambda data: 0)
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    with pytest.raises(RuntimeConfigurationError, match="unavailable"):
        await provider.fetch("cloud", binding())
    await provider.close()


async def test_explicit_zero_checksum_is_valid_when_it_matches(sdk, monkeypatch):
    sdk.responses[:] = [response(crc=0)]
    monkeypatch.setattr(gcp_secrets, "_checksum", lambda data: 0)
    provider = gcp_secrets.GcpSecretProvider(project=_PROJECT)
    result = await provider.fetch("cloud", binding())
    assert result.value.get_secret_value() == "123456789"
    await provider.close()
