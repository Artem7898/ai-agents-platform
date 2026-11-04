"""Structured node results stay objects through durable execution and recovery."""

import copy
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, model_validator

import src.agents.llm_service as llm_module
from src.agents.models import Agent
from src.agents.tool_registry import ToolRegistry
from src.runs import persistence
from src.runs.exceptions import ExecutionLeaseLost
from src.runs.executor import WorkflowExecutor
from src.runs.models import RunEvent, WorkflowRun
from src.runs.schemas import RunCheckpoint
from src.runs.services import create_run
from src.runtimes.models import LLMRuntime
from src.usage.models import LLMInvocation, OwnerQuota
from src.workflows.models import Workflow
from src.workflows.output_schemas import OutputSchemaRegistry, ReviewDecisionV1
from src.workflows.schemas import WorkflowSpec

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]
DECISION = {"approved": True, "score": 0.87}


def completion(content=None, *, usage=True, finish_reason="stop", refusal=None, calls=None):
    message = {"role": "assistant", "content": json.dumps(DECISION) if content is None else content}
    if refusal is not None:
        message["refusal"] = refusal
    if calls is not None:
        message["tool_calls"] = calls
    body = {
        "id": "chatcmpl-structured",
        "object": "chat.completion",
        "created": 1,
        "model": "model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    return httpx.Response(200, json=body)


@pytest_asyncio.fixture
async def run_factory(async_db):
    async def create(*, repairs=2, branch=False, with_tools=False, structured_capability=None):
        owner = await get_user_model().objects.acreate(username=f"structured-{uuid.uuid4()}")
        runtime = None
        if structured_capability is not None:
            runtime = await LLMRuntime.objects.acreate(
                owner=owner,
                name="Explicit capability",
                profile="openai_compatible",
                base_url="https://api.openai.com/v1",
                secret_ref="test-only",
                allowed_models=["model"],
                capabilities={"structured_outputs": structured_capability},
            )
        available = ToolRegistry.get_specs()
        agent = await Agent.objects.acreate(
            owner=owner,
            name="Structured reviewer",
            model_name="model",
            system_prompt="Return an approval decision and score.",
            status="active",
            tool_ids=[str(available[0].id)] if with_tools and available else [],
            runtime=runtime,
        )
        nodes = [
            {
                "id": "review",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
                "output_schema": {"id": "review_decision", "version": 1},
            }
        ]
        edges = []
        if branch:
            nodes.extend(
                [
                    {
                        "id": "route",
                        "kind": "condition",
                        "config": {
                            "condition": {"path": "nodes.review.result.approved", "op": "truthy"}
                        },
                    },
                    {
                        "id": "accepted",
                        "kind": "condition",
                        "config": {
                            "condition": {
                                "path": "nodes.review.result.score",
                                "op": "gte",
                                "value": 0.8,
                            }
                        },
                    },
                    {"id": "rejected", "kind": "hitl_approval"},
                ]
            )
            edges = [
                {"source": "review", "target": "route"},
                {"source": "route", "target": "accepted", "condition": True},
                {"source": "route", "target": "rejected", "condition": False},
            ]
        workflow = await Workflow.objects.acreate(
            owner=owner,
            name=f"Structured workflow {uuid.uuid4()}",
            nodes=nodes,
            edges=edges,
            output_node_id="review",
            limits={"max_output_repairs": repairs, "max_output_tokens": 40},
        )
        return owner, await create_run(workflow.pk, {"prompt": "Review this example"}, owner)

    return create


@pytest_asyncio.fixture
async def provider(monkeypatch, settings):
    settings.LLM_RETRY_BASE_SECONDS = 0
    requests, responses, clients = [], [], []

    def handle(request):
        requests.append(json.loads(request.content))
        return responses.pop(0)

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
    yield responses, requests
    for client in clients:
        await client.close()


async def test_object_result_drives_typed_conditions_and_preserves_output(run_factory, provider):
    _, run = await run_factory(branch=True, with_tools=True)
    provider[0].append(completion())
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert run.output_data["result"] == DECISION
    assert run.checkpoint["context"]["nodes"]["review"]["result"] == DECISION
    assert run.checkpoint["context"]["llm_output_review"] == DECISION
    assert run.checkpoint["context"]["nodes"]["accepted"]["result"] is True
    assert run.checkpoint["skipped"] == ["rejected"]
    assert run.checkpoint["usage"]["total_tokens"] == 10
    call = await LLMInvocation.objects.aget(run=run)
    assert call.operation == "structured" and call.total_tokens == 10
    request = provider[1][0]
    assert request["response_format"]["type"] == "json_schema"
    assert "tools" not in request and "tool_choice" not in request


@pytest.mark.parametrize(
    "content",
    [
        "invalid-private-json-marker",
        '{"approved":"invalid-private-json-marker","score":0.87}',
        '{"approved":true,"score":0.87,"invalid-private-json-marker":42}',
    ],
)
async def test_invalid_result_is_charged_repaired_and_never_persisted(
    run_factory, provider, content
):
    _, run = await run_factory()
    provider[0].extend([completion(content), completion()])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed" and run.output_data["result"] == DECISION
    state = run.checkpoint["node_states"]["review"]
    assert state["output_repairs"] == 1 and state["output_rounds"] == 2
    assert run.checkpoint["usage"]["budget_tokens"] == 20
    assert "invalid-private-json-marker" not in json.dumps(run.checkpoint)
    events = [row.payload async for row in RunEvent.objects.filter(run=run)]
    assert "invalid-private-json-marker" not in json.dumps(events)
    assert "invalid-private-json-marker" not in json.dumps(provider[1][1])
    assert [message["role"] for message in provider[1][1]["messages"]] == ["system", "user", "user"]
    assert await LLMInvocation.objects.filter(run=run, total_tokens=10).acount() == 2


async def test_missing_usage_keeps_hold_for_a_successful_structured_result(run_factory, provider):
    _, run = await run_factory()
    provider[0].append(completion(usage=False))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    call = await LLMInvocation.objects.aget(run=run)
    assert run.status == "completed" and run.output_data["result"] == DECISION
    assert call.total_tokens is None and call.reservation_active
    assert run.checkpoint["usage"]["budget_tokens"] == call.reserved_tokens
    assert run.checkpoint["usage"]["unknown_calls"] == 1


async def test_output_repair_budget_is_terminal_and_usage_is_retained(run_factory, provider):
    _, run = await run_factory(repairs=1)
    provider[0].extend([completion("not-json"), completion('{"approved":"wrong","score":0.87}')])
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(provider[1]) == 2
    state = run.checkpoint["node_states"]["review"]
    assert state["output_repairs"] == 1 and state["output_rounds"] == 2
    assert "result" not in state
    assert "output repair budget exhausted" in run.checkpoint["error"]
    assert run.checkpoint["usage"]["total_tokens"] == 20
    events = [
        row.payload
        async for row in RunEvent.objects.filter(run=run, event_type="output_validation_failed")
    ]
    assert [event["will_retry"] for event in events] == [True, False]


@pytest.mark.parametrize("legacy", [False, True])
async def test_zero_and_omitted_frozen_output_repair_budget_remain_fail_fast(
    run_factory, provider, legacy
):
    _, run = await run_factory(repairs=2 if legacy else 0)
    if legacy:
        snapshot = copy.deepcopy(run.workflow_snapshot)
        del snapshot["limits"]["max_output_repairs"]
        await WorkflowRun.objects.filter(pk=run.pk).aupdate(workflow_snapshot=snapshot)
    provider[0].append(completion("not-json"))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(provider[1]) == 1
    assert run.checkpoint["node_states"]["review"].get("output_repairs", 0) == 0


async def test_owner_quota_prevents_an_additional_structured_repair(run_factory, provider):
    owner, run = await run_factory()
    await OwnerQuota.objects.acreate(owner=owner, request_limit=1)
    provider[0].append(completion("not-json"))
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(provider[1]) == 1
    assert run.checkpoint["usage"]["total_tokens"] == 10
    assert await LLMInvocation.objects.filter(run=run, status="quota_denied").acount() == 1


async def test_checkpointed_output_repair_counter_survives_worker_restart(
    run_factory, provider, monkeypatch
):
    _, run = await run_factory(repairs=1)
    provider[0].extend([completion("not-json"), completion("still-invalid")])
    append = persistence.append_event

    async def crash(run_id, event_type, *args, **kwargs):
        if event_type == "output_validation_failed":
            raise ExecutionLeaseLost("Stopped after saving output correction")
        return await append(run_id, event_type, *args, **kwargs)

    monkeypatch.setattr(persistence, "append_event", crash)
    with pytest.raises(ExecutionLeaseLost):
        await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.checkpoint["node_states"]["review"]["output_repairs"] == 1
    monkeypatch.setattr(persistence, "append_event", append)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(provider[1]) == 2
    assert run.checkpoint["usage"]["total_tokens"] == 20
    assert run.checkpoint["node_states"]["review"]["output_repairs"] == 1


async def test_cancellation_after_saved_correction_does_not_dispatch_again(
    run_factory, provider, monkeypatch
):
    _, run = await run_factory()
    provider[0].append(completion("not-json"))
    append = persistence.append_event

    async def cancel(run_id, event_type, *args, **kwargs):
        event = await append(run_id, event_type, *args, **kwargs)
        if event_type == "output_validation_failed":
            await WorkflowRun.objects.filter(pk=run_id).aupdate(cancel_requested=True)
        return event

    monkeypatch.setattr(persistence, "append_event", cancel)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "cancelled" and len(provider[1]) == 1
    assert run.checkpoint["usage"]["total_tokens"] == 10


async def test_saved_done_result_recovers_as_an_object_without_another_request(
    run_factory, provider
):
    _, run = await run_factory()
    checkpoint = RunCheckpoint(
        context={"input": {"prompt": "Review this example"}, "nodes": {}},
        node_states={
            "review": {"entered": True, "done": True, "result": DECISION, "output_rounds": 1}
        },
    )
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        status="running", checkpoint=checkpoint.model_dump(mode="json")
    )
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed" and provider[1] == []
    assert isinstance(run.output_data["result"], dict)
    assert run.output_data["result"] == DECISION
    assert run.checkpoint["context"]["nodes"]["review"]["result"] == DECISION


async def test_saved_result_custom_validator_failure_is_private_and_never_dispatches(
    run_factory, provider, monkeypatch
):
    _, run = await run_factory()
    monkeypatch.setattr(
        OutputSchemaRegistry, "_definitions", dict(OutputSchemaRegistry._definitions)
    )

    @OutputSchemaRegistry.register("checkpoint_failure", 1)
    class SavedResult(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
        approved: bool
        score: float

        @model_validator(mode="after")
        def reject(self):
            raise RuntimeError(f"private-checkpoint-validator-marker: {self.approved}")

    snapshot = copy.deepcopy(run.workflow_snapshot)
    snapshot["nodes"][0]["output_schema"] = {"id": "checkpoint_failure", "version": 1}
    definitions = OutputSchemaRegistry.snapshot_for_nodes(
        WorkflowSpec.model_validate(snapshot).nodes
    )
    checkpoint = RunCheckpoint(
        context={"input": {"prompt": "Review this example"}, "nodes": {}},
        node_states={"review": {"entered": True, "done": True, "result": DECISION}},
    )
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        status="running",
        workflow_snapshot=snapshot,
        output_schema_snapshots=definitions,
        checkpoint=checkpoint.model_dump(mode="json"),
    )
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and provider[1] == []
    assert "does not match its frozen contract" in run.checkpoint["error"]
    assert "private-checkpoint-validator-marker" not in json.dumps(run.checkpoint)
    events = [row.payload async for row in RunEvent.objects.filter(run=run)]
    assert "private-checkpoint-validator-marker" not in json.dumps(events)


@pytest.mark.parametrize("mutation", ["missing", "fingerprint", "removed", "drift"])
async def test_missing_or_changed_frozen_contract_prevents_provider_dispatch(
    run_factory, provider, monkeypatch, mutation
):
    _, run = await run_factory()
    if mutation in {"missing", "fingerprint"}:
        snapshots = copy.deepcopy(run.output_schema_snapshots)
        if mutation == "missing":
            snapshots.clear()
        else:
            snapshots["review"]["fingerprint"] = "0" * 64
        await WorkflowRun.objects.filter(pk=run.pk).aupdate(output_schema_snapshots=snapshots)
    elif mutation == "removed":
        monkeypatch.setattr(
            OutputSchemaRegistry,
            "_definitions",
            {
                key: value
                for key, value in OutputSchemaRegistry._definitions.items()
                if key != ("review_decision", 1)
            },
        )
    else:
        schema = ReviewDecisionV1.model_json_schema()
        schema["description"] = "Changed without a new version"
        monkeypatch.setattr(
            ReviewDecisionV1, "model_json_schema", classmethod(lambda cls, **kw: schema)
        )
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and provider[1] == []
    assert not await LLMInvocation.objects.filter(run=run).aexists()


async def test_workflow_edit_does_not_change_a_frozen_structured_node(run_factory, provider):
    _, run = await run_factory()
    nodes = copy.deepcopy(run.workflow_snapshot["nodes"])
    nodes[0]["output_schema"]["version"] = 999
    await Workflow.objects.filter(pk=run.workflow_id).aupdate(nodes=nodes)
    provider[0].append(completion())
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "completed" and run.output_data["result"] == DECISION
    assert run.workflow_snapshot["nodes"][0]["output_schema"]["version"] == 1


async def test_unsupported_structured_capability_fails_before_http(run_factory, provider):
    _, run = await run_factory(structured_capability=False)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and provider[1] == []
    assert "does not support structured_outputs" in run.checkpoint["error"]
    assert not await LLMInvocation.objects.filter(run=run).aexists()


@pytest.mark.parametrize("failure", ["tool_calls", "refusal", "truncation", "auth"])
async def test_non_schema_failures_do_not_grant_output_corrections(run_factory, provider, failure):
    _, run = await run_factory()
    if failure == "tool_calls":
        response = completion(
            calls=[
                {
                    "id": "unexpected",
                    "type": "function",
                    "function": {"name": "search_knowledge_base", "arguments": "{}"},
                }
            ]
        )
    elif failure == "refusal":
        response = completion(refusal="private-refusal-marker")
    elif failure == "truncation":
        response = completion(finish_reason="length")
    else:
        response = httpx.Response(401, json={"error": "private-auth-marker"})
    provider[0].append(response)
    await WorkflowExecutor(run.pk).execute()
    await run.arefresh_from_db()
    assert run.status == "failed" and len(provider[1]) == 1
    assert not await RunEvent.objects.filter(
        run=run, event_type="output_validation_failed"
    ).aexists()
    assert "private-" not in json.dumps(run.checkpoint)
