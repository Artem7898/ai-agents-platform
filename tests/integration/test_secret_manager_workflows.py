"""Real workflows resolve external secrets at dispatch; only vendor I/O is replaced."""

import json
from types import SimpleNamespace

import google_crc32c
import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI
from pydantic import SecretStr

from src.agents.llm_service import close_llm_client
from src.runs.executor import WorkflowExecutor
from src.runs.models import RunEvent
from src.runs.services import create_run
from src.runtimes import aws_secrets, clients, gcp_secrets, vault
from src.runtimes.models import LLMRuntime
from src.usage.models import LLMInvocation

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest_asyncio.fixture
async def external_backends(settings, monkeypatch):
    settings.LLM_ALLOWED_BASE_URLS = ["https://llm.example/v1"]
    settings.LLM_SECRET_CACHE_TTL_SECONDS = 0
    settings.LLM_VAULT_URL = "https://vault.example"
    settings.LLM_VAULT_TOKEN = SecretStr("private-bootstrap-vault-token")
    settings.LLM_AWS_REGION = "eu-central-1"
    settings.LLM_GCP_PROJECT = "123456789012"
    settings.LLM_SECRET_BINDINGS = {
        "vault-key": {"backend": "vault", "path": "platform/cloud", "field": "api_key"},
        "aws-key": {"backend": "aws", "secret_id": "platform/cloud", "field": "api_key"},
        "gcp-key": {"backend": "gcp", "secret_id": "cloud", "field": "api_key"},
    }
    settings.LLM_RUNTIME_SECRETS = {
        alias: SecretStr("must-never-be-used-as-fallback") for alias in settings.LLM_SECRET_BINDINGS
    }
    values = {
        backend: {"key": f"private-{backend}-old-key", "version": 1}
        for backend in ("vault", "aws", "gcp")
    }
    failed, fetches, llm_requests = set(), [], []

    def read(backend):
        fetches.append(backend)
        if backend in failed:
            raise RuntimeError("private-sdk-error-" + values[backend]["key"])
        return values[backend]

    def vault_request(request):
        value = read("vault")
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {"api_key": value["key"]},
                    "metadata": {
                        "version": value["version"],
                        "destroyed": False,
                        "deletion_time": "",
                    },
                }
            },
        )

    monkeypatch.setattr(
        vault,
        "_create_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(vault_request), timeout=timeout, follow_redirects=False
        ),
    )

    class AwsClient:
        def get_secret_value(self, **kwargs):
            value = read("aws")
            return {
                "SecretString": json.dumps({"api_key": value["key"]}),
                "VersionId": str(value["version"]),
                "VersionStages": ["AWSCURRENT"],
            }

        def close(self):
            pass

    monkeypatch.setattr(aws_secrets, "_create_client", lambda region, timeout: AwsClient())

    class GcpClient:
        def access_secret_version(self, **kwargs):
            value = read("gcp")
            data = json.dumps({"api_key": value["key"]}).encode()
            return SimpleNamespace(
                name=f"projects/123456789012/secrets/cloud/versions/{value['version']}",
                payload=SimpleNamespace(data=data, data_crc32c=google_crc32c.value(data)),
            )

        def close(self):
            pass

    monkeypatch.setattr(gcp_secrets, "_create_client", GcpClient)

    def llm_request(request):
        llm_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-secret",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            },
        )

    monkeypatch.setattr(
        clients,
        "_create_client",
        lambda base_url, credential: AsyncOpenAI(
            api_key=credential.value.get_secret_value(),
            base_url=base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_request)),
        ),
    )
    yield SimpleNamespace(values=values, failed=failed, fetches=fetches, llm=llm_requests)
    await close_llm_client()


async def workflow_for(backend, agent_factory, workflow_factory):
    owner = await get_user_model().objects.acreate(username=f"{backend}-secret-workflow")
    runtime = await LLMRuntime.objects.acreate(
        owner=owner,
        name=backend,
        profile="openai_compatible",
        base_url="https://llm.example/v1",
        secret_ref=f"{backend}-key",
        allowed_models=["model"],
    )
    agent = await agent_factory(owner=owner, model_name="model", runtime=runtime)
    workflow = await workflow_factory(
        owner=owner,
        nodes=[{"id": "answer", "kind": "llm_call", "agent_id": str(agent.pk)}],
        edges=[],
    )
    return owner, workflow


@pytest.mark.parametrize("backend", ["vault", "aws", "gcp"])
async def test_queued_workflow_observes_external_rotation_without_snapshotting_keys(
    external_backends, backend, agent_factory, workflow_factory, capsys
):
    state = external_backends
    owner, workflow = await workflow_for(backend, agent_factory, workflow_factory)
    first = await create_run(workflow.pk, {"prompt": "first"}, owner)
    queued = await create_run(workflow.pk, {"prompt": "queued"}, owner)
    assert state.fetches == []  # Creation and validation do not contact secret managers.
    await WorkflowExecutor(first.pk).execute()
    state.values[backend] = {"key": f"private-{backend}-rotated-key", "version": 2}
    await WorkflowExecutor(queued.pk).execute()
    await first.arefresh_from_db()
    await queued.arefresh_from_db()
    assert first.status == queued.status == "completed"
    assert state.fetches == [backend, backend]
    assert [request.headers["authorization"] for request in state.llm] == [
        f"Bearer private-{backend}-old-key",
        f"Bearer private-{backend}-rotated-key",
    ]
    assert queued.runtime_snapshots == first.runtime_snapshots
    assert await LLMInvocation.objects.filter(owner=owner, total_tokens=10).acount() == 2
    records = [
        event
        async for event in RunEvent.objects.filter(run=queued).values("payload", "context_snapshot")
    ]
    persisted = json.dumps(
        [queued.runtime_snapshots, queued.agent_snapshots, queued.checkpoint, records]
    )
    output = capsys.readouterr()
    for marker in (
        f"private-{backend}-old-key",
        f"private-{backend}-rotated-key",
        "private-bootstrap-vault-token",
        "must-never-be-used-as-fallback",
    ):
        assert marker not in persisted + output.out + output.err


@pytest.mark.parametrize("backend", ["vault", "aws", "gcp"])
async def test_external_denial_blocks_workflow_before_llm_and_never_uses_settings_fallback(
    external_backends, backend, agent_factory, workflow_factory
):
    state = external_backends
    owner, workflow = await workflow_for(backend, agent_factory, workflow_factory)
    warmed = await create_run(workflow.pk, {"prompt": "warm"}, owner)
    await WorkflowExecutor(warmed.pk).execute()
    state.failed.add(backend)
    denied = await create_run(workflow.pk, {"prompt": "blocked"}, owner)
    await WorkflowExecutor(denied.pk).execute()
    await denied.arefresh_from_db()
    assert denied.status == "failed"
    assert len(state.llm) == 1
    assert not await LLMInvocation.objects.filter(run=denied).aexists()
    events = [event.payload async for event in RunEvent.objects.filter(run=denied)]
    payload = json.dumps([denied.checkpoint, events])
    assert "private-sdk-error" not in payload
    assert state.values[backend]["key"] not in payload
    assert "must-never-be-used-as-fallback" not in payload
