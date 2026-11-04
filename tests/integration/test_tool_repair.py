"""Whole-batch repair, recovery and quota checks against the real engine/ledger."""

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI

import src.agents.llm_service as llm_module
from src.agents.models import Agent
from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolRegistry
from src.runs import persistence
from src.runs.exceptions import ExecutionLeaseLost
from src.runs.executor import WorkflowExecutor
from src.runs.models import RunEvent, WorkflowRun
from src.runs.schemas import RunCheckpoint
from src.runs.services import create_run
from src.usage.models import LLMInvocation, OwnerQuota
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def tool_call(call_id="call_1", arguments=None, name="observed"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"value": 7} if arguments is None else arguments),
        },
    }


def completion(*calls):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-repair",
            "object": "chat.completion",
            "created": 1,
            "model": "model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None if calls else "answer",
                        "tool_calls": list(calls),
                    },
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        },
    )


@pytest.fixture
def registry(monkeypatch):
    for name in ("_executors", "_definitions", "_argument_models", "_context_parameters"):
        monkeypatch.setattr(ToolRegistry, name, {})
    executions = []

    @ToolRegistry.register(description="Observe a typed value and workflow identity")
    async def observed(value: int, context: ToolExecutionContext) -> int:
        executions.append((value, context))
        return value

    @ToolRegistry.register(description="A tool whose backend fails")
    async def broken(value: int) -> int:
        executions.append((value, None))
        raise ValueError("Backend failed after dispatch")

    return executions


@pytest_asyncio.fixture
async def run_factory(async_db, registry):
    async def create(*, repairs=2, max_calls=4, tool_name="observed", explicit=False):
        owner = await get_user_model().objects.acreate(username=f"repair-{uuid.uuid4()}")
        spec = next(item for item in ToolRegistry.get_specs() if item.name == tool_name)
        agent = await Agent.objects.acreate(
            owner=owner,
            name="Repair agent",
            model_name="model",
            system_prompt="Use the available tool when needed.",
            status="active",
            tool_ids=[str(spec.id)],
        )
        node = (
            {
                "id": "ask",
                "kind": "tool_exec",
                "tool_id": str(spec.id),
                "config": {"arguments": {"value": 5}},
            }
            if explicit
            else {"id": "ask", "kind": "llm_call", "agent_id": str(agent.pk)}
        )
        workflow = await Workflow.objects.acreate(
            owner=owner,
            name=f"Repair workflow {uuid.uuid4()}",
            nodes=[node],
            edges=[],
            limits={
                "max_tool_calls": max_calls,
                "max_tool_repairs": repairs,
                "max_output_tokens": 10,
            },
        )
        return owner, await create_run(workflow.pk, {"prompt": "Question"}, owner)

    return create


@pytest_asyncio.fixture
async def provider(monkeypatch, settings):
    settings.LLM_RETRY_BASE_SECONDS = 0
    requests, responses, clients = [], [], []

    def handle(request):
        requests.append(json.loads(request.content))
        response = responses.pop(0)
        return response() if callable(response) else response

    def connect():
        client = AsyncOpenAI(
            api_key="test-only",
            base_url="https://provider.example/v1",
            max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(llm_module, "get_llm_client", connect)
    yield responses, requests
    for client in clients:
        await client.close()


async def test_mixed_batch_executes_nothing_until_all_arguments_are_valid(
    run_factory, registry, provider
):
    owner, run = await run_factory(max_calls=2)
    first = completion(tool_call("discarded_good"), tool_call("discarded_bad", {"value": "7"}))

    def repaired():
        assert registry == []
        return completion(tool_call("accepted_1"), tool_call("accepted_2", {"value": 8}))

    provider[0].extend([first, repaired, completion()])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert [value for value, _ in registry] == [7, 8]
    for _, context in registry:
        assert context.run_id == run.pk and context.owner_id == owner.pk
        assert context.workflow_id == run.workflow_id and context.node_id == "ask"
        assert context.idempotency_key == f"{run.pk}:ask:{context.tool_call_id}"
    assert run.checkpoint["node_states"]["ask"]["repairs"] == 1
    assert run.checkpoint["node_states"]["ask"]["rounds"] == 3
    assert run.checkpoint["usage"]["total_tokens"] == 30
    assert await LLMInvocation.objects.filter(run=run).acount() == 3
    messages = provider[1][1]["messages"]
    assert [item["role"] for item in messages] == ["system", "user", "user"]
    assert not any("discarded" in json.dumps(item) for item in messages)


async def test_malformed_json_is_repaired_without_persisting_rejected_arguments(
    run_factory, registry, provider
):
    _, run = await run_factory(max_calls=1)
    malformed = tool_call()
    secret = "invalid-json-private-value"
    malformed["function"]["arguments"] = secret
    provider[0].extend([completion(malformed), completion(tool_call("fixed")), completion()])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert len(registry) == 1
    assert run.checkpoint["usage"]["budget_tokens"] == 30
    assert secret not in json.dumps(run.checkpoint)
    events = [row.payload async for row in RunEvent.objects.filter(run=run)]
    assert secret not in json.dumps(events)
    assert secret not in json.dumps(provider[1][1]["messages"])
    validation = await RunEvent.objects.aget(run=run, event_type="tool_validation_failed")
    assert validation.payload["issues"] == [
        {"code": "invalid_tool_arguments", "path": ["arguments"]}
    ]


async def test_repair_budget_exhaustion_is_durable_and_records_all_usage(
    run_factory, registry, provider
):
    _, run = await run_factory(repairs=1)
    provider[0].extend(
        [completion(tool_call("bad_1", {"value": "7"})), completion(tool_call("bad_2", {}))]
    )
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == []
    state = run.checkpoint["node_states"]["ask"]
    assert state["repairs"] == 1 and state["rounds"] == 2
    assert run.checkpoint["usage"]["total_tokens"] == 20
    assert "repair budget exhausted" in run.checkpoint["error"]
    events = [
        row.payload
        async for row in RunEvent.objects.filter(run=run, event_type="tool_validation_failed")
    ]
    assert [row["will_retry"] for row in events] == [True, False]


@pytest.mark.parametrize("legacy", [False, True])
async def test_zero_repairs_and_legacy_snapshots_fail_without_another_request(
    run_factory, registry, provider, legacy
):
    _, run = await run_factory(repairs=0 if not legacy else 2)
    if legacy:
        snapshot = run.workflow_snapshot
        del snapshot["limits"]["max_tool_repairs"]
        await WorkflowRun.objects.filter(pk=run.pk).aupdate(workflow_snapshot=snapshot)
    provider[0].append(completion(tool_call(arguments={"value": "invalid"})))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == []
    assert len(provider[1]) == 1
    assert run.checkpoint["node_states"]["ask"].get("repairs", 0) == 0


async def test_forbidden_tool_with_malformed_arguments_is_fatal_not_repaired(
    run_factory, registry, provider
):
    _, run = await run_factory()
    forbidden = tool_call(name="broken")
    forbidden["function"]["arguments"] = "private-invalid-json"
    provider[0].append(completion(forbidden))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == []
    assert len(provider[1]) == 1
    assert not await RunEvent.objects.filter(run=run, event_type="tool_validation_failed").aexists()


async def test_dispatched_tool_errors_are_never_repaired(run_factory, registry, provider):
    _, run = await run_factory(tool_name="broken")
    provider[0].append(completion(tool_call(name="broken")))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(registry) == 1
    assert len(provider[1]) == 1
    assert not await RunEvent.objects.filter(run=run, event_type="tool_validation_failed").aexists()


async def test_reused_tool_call_identifier_is_rejected_before_reexecution(
    run_factory, registry, provider
):
    _, run = await run_factory()
    provider[0].extend([completion(tool_call("reused")), completion(tool_call("reused"))])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(registry) == 1
    assert len(provider[1]) == 2
    assert "reused a tool call identifier" in run.checkpoint["error"]


async def test_owner_quota_blocks_repair_before_another_http_request(
    run_factory, registry, provider
):
    owner, run = await run_factory()
    await OwnerQuota.objects.acreate(owner=owner, request_limit=1)
    provider[0].append(completion(tool_call(arguments={"value": "invalid"})))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == [] and len(provider[1]) == 1
    assert await LLMInvocation.objects.filter(run=run, status="quota_denied").acount() == 1
    assert run.checkpoint["usage"]["total_tokens"] == 10


async def test_restart_cannot_reset_persisted_repair_counter(
    run_factory, registry, provider, monkeypatch
):
    _, run = await run_factory(repairs=1)
    provider[0].extend(
        [completion(tool_call("bad_1", {"value": "invalid"})), completion(tool_call("bad_2", {}))]
    )
    append = persistence.append_event

    async def crash_after_saved_feedback(run_id, event_type, *args, **kwargs):
        if event_type == "tool_validation_failed":
            raise ExecutionLeaseLost("Worker stopped after repair checkpoint")
        return await append(run_id, event_type, *args, **kwargs)

    monkeypatch.setattr(persistence, "append_event", crash_after_saved_feedback)
    with pytest.raises(ExecutionLeaseLost):
        await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "running"
    assert run.checkpoint["node_states"]["ask"]["repairs"] == 1
    monkeypatch.setattr(persistence, "append_event", append)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == [] and len(provider[1]) == 2
    assert run.checkpoint["node_states"]["ask"]["repairs"] == 1
    assert run.checkpoint["usage"]["total_tokens"] == 20


async def test_potentially_dispatched_saved_batch_cannot_be_regenerated(
    run_factory, registry, provider
):
    _, run = await run_factory()
    call = {"id": "saved", "name": "observed", "arguments": {"value": "invalid"}}
    checkpoint = RunCheckpoint(
        context={"input": {"prompt": "Question"}, "nodes": {}},
        tool_calls=1,
        reserved_tool_calls=["ask:saved"],
        node_states={
            "ask": {
                "entered": True,
                "history": [{"role": "assistant", "content": None, "tool_calls": [call]}],
                "pending_calls": [call],
                "next_call": 0,
                "rounds": 1,
            }
        },
    )
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        checkpoint=checkpoint.model_dump(mode="json")
    )
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == [] and provider[1] == []
    assert "partially executed tool batch" in run.checkpoint["error"]


async def test_batch_exceeding_remaining_tool_budget_executes_nothing(
    run_factory, registry, provider
):
    _, run = await run_factory(max_calls=1)
    provider[0].append(completion(tool_call("one"), tool_call("two")))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and registry == [] and len(provider[1]) == 1
    assert "tool-call budget" in run.checkpoint["error"]


async def test_explicit_tool_nodes_receive_the_same_trusted_context(
    run_factory, registry, provider
):
    owner, run = await run_factory(explicit=True)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed" and provider[1] == []
    value, context = registry[0]
    assert value == 5 and context.owner_id == owner.pk
    assert context.run_id == run.pk and context.tool_call_id == "tool"
    assert context.idempotency_key == f"{run.pk}:ask:tool"
