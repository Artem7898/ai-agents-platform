"""Runtime ownership and real workflow switching, with only HTTP mocked."""

import json

import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from nova.core.exceptions import NovaValidationError
from openai import AsyncOpenAI
from pydantic import SecretStr
from rest_framework.authtoken.models import Token

import src.runtimes.clients as runtime_clients
from src.agents.llm_service import close_llm_client
from src.agents.models import Agent
from src.runs.management.commands.runworker import executor_handler
from src.runs.models import OutboxMessage, RunEvent
from src.runs.outbox import OutboxWorker
from src.runs.services import create_run
from src.runtimes.models import LLMRuntime

pytestmark = pytest.mark.django_db(transaction=True)


@pytest_asyncio.fixture(autouse=True)
async def runtime_cleanup():
    yield
    await close_llm_client()


@pytest.fixture
def runtime_settings(settings):
    settings.LLM_ALLOWED_BASE_URLS = [
        "https://cloud.example/v1",
        "https://replacement.example/v1",
        "http://localhost:11434/v1",
    ]
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("mock-cloud-old-key")}
    return settings


async def user_with_token(name):
    user = await get_user_model().objects.acreate(username=name)
    token = await Token.objects.acreate(user=user)
    return user, {"Authorization": f"Token {token.key}"}


async def runtime_for(owner, *, local=False):
    return await LLMRuntime.objects.acreate(
        owner=owner,
        name="Local" if local else "Cloud",
        profile="ollama" if local else "openai_compatible",
        base_url="http://localhost:11434/v1" if local else "https://cloud.example/v1",
        secret_ref=None if local else "cloud",
        allowed_models=["model"],
        capabilities={"token_limit_parameter": "max_tokens"},
    )


@pytest.fixture
def provider_http(monkeypatch):
    received = []

    def reply(request):
        body = json.loads(request.content)
        received.append((request, body))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-runtime",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"{request.url.host}:{body['messages'][-1]['content']}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    def make_client(base_url, credential):
        return AsyncOpenAI(
            base_url=base_url,
            api_key=credential.value.get_secret_value(),
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(reply), follow_redirects=False
            ),
        )

    monkeypatch.setattr(runtime_clients, "_create_client", make_client)
    return received


async def test_agent_assignment_checks_owner_model_and_enablement(async_client, runtime_settings):
    owner, headers = await user_with_token("assignment-owner")
    other, _ = await user_with_token("assignment-other")
    own = await runtime_for(owner)
    foreign = await runtime_for(other)
    values = {
        "name": "Agent",
        "model_name": "model",
        "system_prompt": "Assist",
        "status": "active",
        "runtime_id": str(own.pk),
    }
    response = await async_client.post("/api/v1/agents/", headers=headers, json=values)
    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]
    assert response.json()["runtime_id"] == str(own.pk)
    denied = await async_client.patch(
        f"/api/v1/agents/{agent_id}/", headers=headers, json={"runtime_id": str(foreign.pk)}
    )
    assert denied.status_code == 400
    denied_model = await async_client.patch(
        f"/api/v1/agents/{agent_id}/", headers=headers, json={"model_name": "not-approved"}
    )
    assert denied_model.status_code == 400
    await LLMRuntime.objects.filter(pk=own.pk).aupdate(enabled=False)
    denied_disabled = await async_client.patch(
        f"/api/v1/agents/{agent_id}/", headers=headers, json={"system_prompt": "changed"}
    )
    assert denied_disabled.status_code == 400
    agent = await Agent.objects.aget(pk=agent_id)
    assert agent.runtime_id == own.pk
    assert agent.model_name == "model"


async def test_direct_orm_cannot_assign_foreign_runtime(runtime_settings, agent_factory):
    owner, _ = await user_with_token("direct-owner")
    other, _ = await user_with_token("direct-other")
    foreign = await runtime_for(other)
    with pytest.raises(NovaValidationError, match="runtime"):
        await agent_factory(owner=owner, model_name="model", runtime_id=foreign.pk)
    assert not await Agent.objects.filter(owner=owner).aexists()


async def test_workflow_switches_local_cloud_and_rotated_credentials_without_changing_accepted_run(
    async_client, runtime_settings, provider_http, agent_factory, workflow_factory
):
    owner, headers = await user_with_token("switch-owner")
    local = await runtime_for(owner, local=True)
    cloud = await runtime_for(owner)
    agent = await agent_factory(owner=owner, model_name="model", runtime_id=local.pk)
    workflow = await workflow_factory(
        owner=owner,
        nodes=[
            {
                "id": "answer",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
                "config": {"prompt_template": "{input.prompt}"},
            }
        ],
        edges=[],
    )
    accepted_local = await create_run(workflow.pk, {"prompt": "local"}, owner)
    changed = await async_client.patch(
        f"/api/v1/agents/{agent.pk}/", headers=headers, json={"runtime_id": str(cloud.pk)}
    )
    assert changed.status_code == 200, changed.text
    accepted_cloud = await create_run(workflow.pk, {"prompt": "cloud"}, owner)
    assert await OutboxWorker.process_next_batch(executor_handler) == 2
    await accepted_local.arefresh_from_db()
    await accepted_cloud.arefresh_from_db()
    assert accepted_local.status == accepted_cloud.status == "completed"
    assert accepted_local.output_data["result"] == "localhost:local"
    assert accepted_cloud.output_data["result"] == "cloud.example:cloud"
    assert accepted_local.agent_snapshots[str(agent.pk)]["spec"]["runtime_id"] == str(local.pk)
    assert accepted_cloud.runtime_snapshots[str(cloud.pk)]["base_url"] == cloud.base_url
    assert provider_http[0][0].headers["authorization"] == "Bearer ollama-local-no-auth"
    assert provider_http[1][0].headers["authorization"] == "Bearer mock-cloud-old-key"
    runtime_settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("mock-cloud-rotated-key")}
    third = await create_run(workflow.pk, {"prompt": "rotated"}, owner)
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await third.arefresh_from_db()
    assert third.status == "completed"
    assert provider_http[2][0].headers["authorization"] == "Bearer mock-cloud-rotated-key"
    records = [
        event
        async for event in RunEvent.objects.filter(run=third).values("payload", "context_snapshot")
    ]
    persisted = json.dumps(
        [third.runtime_snapshots, third.agent_snapshots, third.checkpoint, records], default=str
    )
    assert "mock-cloud-old-key" not in persisted
    assert "mock-cloud-rotated-key" not in persisted
    assert await OutboxMessage.objects.filter(status="completed").acount() == 3


async def test_runtime_config_is_frozen_but_secret_is_not_snapshotted(
    runtime_settings, provider_http, agent_factory, workflow_factory
):
    owner, _ = await user_with_token("freeze-runtime-owner")
    runtime = await runtime_for(owner)
    agent = await agent_factory(owner=owner, model_name="model", runtime_id=runtime.pk)
    workflow = await workflow_factory(
        owner=owner,
        nodes=[
            {
                "id": "answer",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
                "config": {"prompt_template": "frozen"},
            }
        ],
        edges=[],
    )
    run = await create_run(workflow.pk, {}, owner)
    runtime.base_url = "https://replacement.example/v1"
    await runtime.asave()
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert provider_http[0][0].url.host == "cloud.example"
    assert run.runtime_snapshots[str(runtime.pk)]["revision"] < runtime.revision
    assert "api_key" not in json.dumps(run.runtime_snapshots)


@pytest.mark.parametrize("revocation", ["disabled", "owner", "model", "endpoint"])
async def test_runtime_revocation_blocks_already_accepted_run_without_provider_traffic(
    runtime_settings, provider_http, agent_factory, workflow_factory, revocation
):
    owner, _ = await user_with_token("revoke-owner")
    runtime = await runtime_for(owner)
    agent = await agent_factory(owner=owner, model_name="model", runtime_id=runtime.pk)
    workflow = await workflow_factory(
        owner=owner,
        nodes=[{"id": "answer", "kind": "llm_call", "agent_id": str(agent.pk)}],
        edges=[],
    )
    run = await create_run(workflow.pk, {"prompt": "blocked"}, owner)
    if revocation == "disabled":
        await LLMRuntime.objects.filter(pk=runtime.pk).aupdate(enabled=False)
    elif revocation == "owner":
        other, _ = await user_with_token("new-runtime-owner")
        await LLMRuntime.objects.filter(pk=runtime.pk).aupdate(owner=other)
    elif revocation == "model":
        await LLMRuntime.objects.filter(pk=runtime.pk).aupdate(allowed_models=["other"])
    else:
        runtime_settings.LLM_ALLOWED_BASE_URLS = []
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    assert run.status == "failed"
    assert provider_http == []


async def test_revocation_between_api_validation_and_model_save_is_controlled(
    async_client, runtime_settings, monkeypatch
):
    import src.api.drf.viewsets as viewsets

    owner, headers = await user_with_token("save-race-owner")
    runtime = await runtime_for(owner)
    original = viewsets.validate_runtime_for_agent

    def revoke_after_validation(candidate, owner_id, agent):
        result = original(candidate, owner_id, agent)
        LLMRuntime.objects.filter(pk=candidate.pk).update(enabled=False)
        return result

    monkeypatch.setattr(viewsets, "validate_runtime_for_agent", revoke_after_validation)
    response = await async_client.post(
        "/api/v1/agents/",
        headers=headers,
        json={
            "name": "Race",
            "model_name": "model",
            "system_prompt": "Assist",
            "runtime_id": str(runtime.pk),
        },
    )
    assert response.status_code == 400, response.text
    assert "runtime_id" in response.json()
    assert not await Agent.objects.filter(owner=owner).aexists()
