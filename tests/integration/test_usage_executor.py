"""Real executor and ledger integration; only provider HTTP is replaced."""

import uuid

import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI

import src.agents.llm_service as llm_module
from src.agents.models import Agent
from src.agents.tool_registry import ToolRegistry
from src.runs.executor import WorkflowExecutor
from src.runs.models import WorkflowRun
from src.runs.schemas import RunCheckpoint
from src.runs.services import create_run
from src.usage import services
from src.usage.contracts import InvocationContext
from src.usage.models import LLMInvocation
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def completion(*, usage=True, invalid=False):
    body = {
        "id": "chatcmpl-metered",
        "object": "chat.completion",
        "created": 1,
        "model": "model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    if invalid:
        body["choices"][0]["message"] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "bad-call",
                    "type": "function",
                    "function": {"name": "search_knowledge_base", "arguments": "not-json"},
                }
            ],
        }
    return httpx.Response(200, json=body)


@pytest_asyncio.fixture
async def run_factory(async_db):
    async def create(*, max_tokens=32000, checkpoint=None, max_tool_repairs=2, tool_ids=None):
        owner = await get_user_model().objects.acreate(username=f"engine-{uuid.uuid4()}")
        agent = await Agent.objects.acreate(
            owner=owner,
            name="Metered",
            model_name="model",
            status="active",
            system_prompt="Answer briefly",
            tool_ids=tool_ids or [],
        )
        workflow = await Workflow.objects.acreate(
            owner=owner,
            name=f"workflow-{uuid.uuid4()}",
            nodes=[{"id": "ask", "kind": "llm_call", "agent_id": str(agent.pk)}],
            edges=[],
            limits={
                "max_tokens": max_tokens,
                "max_output_tokens": 10,
                "max_tool_repairs": max_tool_repairs,
            },
        )
        run = await create_run(workflow.pk, {"prompt": "Hello"}, owner)
        if checkpoint is not None:
            await WorkflowRun.objects.filter(pk=run.pk).aupdate(checkpoint=checkpoint)
        return owner, run

    return create


@pytest.fixture
def provider(monkeypatch, settings):
    settings.LLM_RETRY_BASE_SECONDS = 0
    clients = []
    received = []
    responses = []

    def handle(request):
        received.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def connect():
        client = AsyncOpenAI(
            api_key="test-only-key",
            base_url="https://provider.example/v1",
            max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(llm_module, "get_llm_client", connect)
    return responses, received, clients


@pytest_asyncio.fixture(autouse=True)
async def close_provider_clients(provider):
    yield
    for client in provider[2]:
        await client.close()


async def test_real_workflow_records_frozen_owner_and_reported_usage(run_factory, provider):
    owner, run = await run_factory()
    provider[0].append(completion())
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    call = await LLMInvocation.objects.aget(run=run)
    assert run.billing_owner_id == getattr(call, "owner_id") == owner.pk
    assert run.status == "completed"
    assert run.output_data["result"] == "answer"
    assert (call.status, call.total_tokens, call.reservation_active) == ("succeeded", 10, False)
    assert run.checkpoint["usage"]["budget_tokens"] == 10
    assert run.checkpoint["usage"]["total_tokens"] == 10
    assert len(provider[1]) == 1


async def test_invalid_tool_arguments_still_persist_usage_in_failed_run(run_factory, provider):
    search = next(spec for spec in ToolRegistry.get_specs() if spec.name == "search_knowledge_base")
    _, run = await run_factory(max_tool_repairs=0, tool_ids=[str(search.id)])
    provider[0].append(completion(invalid=True))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    call = await LLMInvocation.objects.aget(run=run)
    assert call.total_tokens == 10
    assert call.status == "succeeded"
    assert run.status == "failed"
    assert "repair budget exhausted" in run.checkpoint["error"]
    assert run.checkpoint["usage"]["budget_tokens"] == 10
    assert run.checkpoint["usage"]["total_tokens"] == 10


async def test_transient_failure_and_success_both_count_in_checkpoint(run_factory, provider):
    _, run = await run_factory()
    provider[0].extend([httpx.Response(503, json={"error": "unavailable"}), completion()])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    calls = [call async for call in LLMInvocation.objects.filter(run=run).order_by("attempt")]
    assert run.status == "completed"
    assert len(provider[1]) == len(calls) == 2
    assert calls[0].reservation_active
    assert calls[0].total_tokens is None
    assert run.checkpoint["usage"]["budget_tokens"] == calls[0].reserved_tokens + 10
    assert run.checkpoint["usage"]["total_tokens"] == 10
    assert run.checkpoint["usage"]["unknown_calls"] == 1


async def test_missing_usage_is_held_and_projected_without_fabricated_total(run_factory, provider):
    _, run = await run_factory()
    provider[0].append(completion(usage=False))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    call = await LLMInvocation.objects.aget(run=run)
    assert run.status == "completed"
    assert call.total_tokens is None
    assert call.reservation_active
    assert run.checkpoint["usage"]["budget_tokens"] == call.reserved_tokens
    assert run.checkpoint["usage"]["unknown_calls"] == 1
    assert run.checkpoint["usage"]["total_tokens"] == 0


@pytest.mark.parametrize("unknown", [False, True])
async def test_restarted_worker_honors_uncheckpointed_provider_spend(
    run_factory, provider, unknown
):
    _, run = await run_factory(max_tokens=1000)
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(status="running")
    reserved = await services.reserve(
        InvocationContext(run.pk, "ask", 0),
        model_name="model",
        provider="openai",
        estimated_tokens=1000,
        max_output_tokens=10,
        operation="tools",
    )
    await services.mark_dispatched(reserved.id)
    await services.finalize(
        reserved.id,
        status="unknown" if unknown else "succeeded",
        usage=None
        if unknown
        else {"prompt_tokens": 999, "completion_tokens": 1, "total_tokens": 1000},
    )
    # The provider completed, but the previous process died before checkpointing.
    assert run.checkpoint == {}
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert provider[1] == []
    assert run.status == "failed"
    assert run.checkpoint["usage"]["budget_tokens"] == 1000
    assert run.checkpoint["usage"]["unknown_calls"] == int(unknown)
    assert "token budget" in run.checkpoint["error"]


async def test_run_budget_checks_retries_before_second_http_request(run_factory, provider):
    _, run = await run_factory(max_tokens=1000)
    provider[0].append(httpx.Response(503, json={"error": "unavailable"}))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    calls = [call async for call in LLMInvocation.objects.filter(run=run).order_by("attempt")]
    assert run.status == "failed"
    assert len(provider[1]) == 1
    assert len(calls) == 2
    assert calls[1].status == "quota_denied"
    assert run.checkpoint["usage"]["budget_tokens"] == calls[0].reserved_tokens
    assert run.checkpoint["usage"]["unknown_calls"] == 1


async def test_legacy_checkpoint_spend_becomes_durable_baseline(run_factory, provider):
    checkpoint = RunCheckpoint(
        context={"input": {"prompt": "Hello"}, "nodes": {}}, usage={"total_tokens": 999}
    ).model_dump(mode="json")
    _, run = await run_factory(max_tokens=1000, checkpoint=checkpoint)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert provider[1] == []
    assert run.status == "failed"
    assert run.checkpoint["usage"]["ledger_base_tokens"] == 999
    assert run.checkpoint["usage"]["budget_tokens"] == 999
    assert run.checkpoint["usage"]["total_tokens"] == 999


async def test_human_resume_rebuilds_totals_without_double_counting(run_factory, provider):
    _, run = await run_factory()
    snapshot = run.workflow_snapshot
    snapshot["nodes"].extend(
        [
            {"id": "approve", "kind": "hitl_approval"},
            {**snapshot["nodes"][0], "id": "ask_again"},
        ]
    )
    snapshot["edges"] = [
        {"source": "ask", "target": "approve"},
        {"source": "approve", "target": "ask_again"},
    ]
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(workflow_snapshot=snapshot)
    provider[0].extend([completion(), completion()])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "waiting_human"
    assert run.checkpoint["usage"]["budget_tokens"] == 10
    await WorkflowExecutor(run.pk).resume({"node_id": "approve", "input": {"approved": True}})
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert len(provider[1]) == 2
    assert run.checkpoint["usage"]["ledger_base_tokens"] == 0
    assert run.checkpoint["usage"]["budget_tokens"] == 20
    assert run.checkpoint["usage"]["total_tokens"] == 20
