"""GGUF-compatible endpoints use the existing worker, secrets and usage ledger.

Only provider HTTP is replaced; these tests do not load weights or measure GPUs.
"""

import json

import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI
from pydantic import SecretStr

import src.runtimes.clients as clients
from src.agents.llm_service import close_llm_client
from src.runs.management.commands.runworker import executor_handler
from src.runs.models import RunEvent
from src.runs.outbox import OutboxWorker
from src.runs.services import create_run
from src.runtimes.models import LLMRuntime
from src.usage.models import LLMInvocation

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]
MODEL_ALIAS = "qwen-pilot-q4km-test-version-1"


@pytest_asyncio.fixture
async def endpoint(monkeypatch, settings):
    settings.LLM_ALLOWED_BASE_URLS = ["https://gguf.example/v1"]
    settings.LLM_RUNTIME_SECRETS = {"gguf-pilot": SecretStr("test-only-gguf-access-key")}
    requests, options = [], {"usage": True}

    def reply(request):
        body = json.loads(request.content)
        requests.append((request, body))
        response = {
            "id": "chatcmpl-gguf-test",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL_ALIAS,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Проверенный тестовый ответ"},
                    "finish_reason": "stop",
                }
            ],
        }
        if options["usage"]:
            response["usage"] = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
        return httpx.Response(200, json=response)

    def connect(base_url, credential):
        return AsyncOpenAI(
            base_url=base_url,
            api_key=credential.value.get_secret_value(),
            max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)),
        )

    monkeypatch.setattr(clients, "_create_client", connect)
    yield requests, options
    await close_llm_client()


async def make_pilot_workflow(agent_factory, workflow_factory, *, structured=False):
    owner = await get_user_model().objects.acreate(username="gguf-pilot-owner")
    runtime = await LLMRuntime.objects.acreate(
        owner=owner,
        name="Pinned GGUF pilot",
        profile="openai_compatible",
        base_url="https://gguf.example/v1",
        secret_ref="gguf-pilot",
        allowed_models=[MODEL_ALIAS],
        capabilities={"token_limit_parameter": "max_tokens"},
    )
    agent = await agent_factory(owner=owner, runtime=runtime, model_name=MODEL_ALIAS)
    node = {"id": "answer", "kind": "llm_call", "agent_id": str(agent.pk)}
    if structured:
        node["output_schema"] = {"id": "review_decision", "version": 1}
    workflow = await workflow_factory(owner=owner, nodes=[node], edges=[])
    return owner, runtime, workflow


@pytest.mark.parametrize("reported_usage", [True, False])
async def test_gguf_workflow_uses_authenticated_compatible_wire_format_and_ledger(
    endpoint, agent_factory, workflow_factory, reported_usage
):
    requests, options = endpoint
    options["usage"] = reported_usage
    owner, runtime, workflow = await make_pilot_workflow(agent_factory, workflow_factory)
    run = await create_run(workflow.pk, {"prompt": "Ответь кратко"}, owner)
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert run.output_data["result"] == "Проверенный тестовый ответ"
    assert len(requests) == 1
    request, body = requests[0]
    assert request.headers["authorization"] == "Bearer test-only-gguf-access-key"
    assert body["model"] == MODEL_ALIAS
    assert body["max_tokens"] == workflow.to_spec().limits.max_output_tokens
    assert "max_completion_tokens" not in body and "tools" not in body
    row = await LLMInvocation.objects.aget(run=run)
    assert row.runtime_id == runtime.pk and row.owner_id == owner.pk
    assert row.provider == "openai_compatible"
    if reported_usage:
        assert row.total_tokens == run.checkpoint["usage"]["budget_tokens"] == 20
        assert not row.reservation_active
    else:
        assert row.total_tokens is None and row.reservation_active
        assert run.checkpoint["usage"]["budget_tokens"] == row.reserved_tokens
        assert run.checkpoint["usage"]["unknown_calls"] == 1
    events = [event.payload async for event in RunEvent.objects.filter(run=run)]
    assert "test-only-gguf-access-key" not in json.dumps(
        [run.runtime_snapshots, run.agent_snapshots, run.checkpoint, events]
    )


async def test_unverified_gguf_structured_capability_fails_before_provider_contact(
    endpoint, agent_factory, workflow_factory
):
    owner, _, workflow = await make_pilot_workflow(agent_factory, workflow_factory, structured=True)
    run = await create_run(workflow.pk, {"prompt": "Review"}, owner)
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    assert run.status == "failed"
    assert "structured_outputs" in run.checkpoint["error"]
    assert endpoint[0] == []
    assert not await LLMInvocation.objects.filter(run=run).aexists()
