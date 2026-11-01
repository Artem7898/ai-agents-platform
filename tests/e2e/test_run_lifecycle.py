"""Real HTTP + ORM + outbox + executor; only the paid LLM boundary is mocked."""

import json

import httpx
import pytest
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI
from rest_framework.authtoken.models import Token

from src.agents import llm_service
from src.runs.management.commands.runworker import executor_handler
from src.runs.models import OutboxMessage, RunEvent, WorkflowRun
from src.runs.outbox import OutboxWorker

pytestmark = pytest.mark.django_db(transaction=True)


async def identity(name):
    user = await get_user_model().objects.acreate(username=name)
    token = await Token.objects.acreate(user=user)
    return user, {"Authorization": f"Token {token.key}"}


async def test_request_to_worker_to_saved_llm_result(
    async_client, agent_factory, workflow_factory, monkeypatch
):
    user, headers = await identity("lifecycle")
    agent = await agent_factory(owner=user, status="active")
    workflow = await workflow_factory(
        owner=user,
        nodes=[
            {
                "id": "answer",
                "kind": "llm_call",
                "agent_id": str(agent.id),
                "config": {"prompt_template": "Question: {prompt}"},
            }
        ],
        edges=[],
    )
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Saved answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = AsyncOpenAI(
        api_key="test-local-only",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(llm_service, "get_llm_client", lambda: client)
    try:
        response = await async_client.post(
            "/api/v2/runs/execute",
            headers=headers,
            json={"workflow_id": str(workflow.id), "input_data": {"prompt": "original question"}},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        # Definition edits after acceptance cannot change this run's execution.
        await (
            type(agent)
            .objects.filter(pk=agent.pk)
            .aupdate(system_prompt="Changed after acceptance")
        )
        assert await OutboxWorker.process_next_batch(executor_handler) == 1
        run = await WorkflowRun.objects.aget(pk=run_id)
        assert run.status == "completed"
        assert run.output_data["result"] == "Saved answer"
        assert run.checkpoint["usage"]["total_tokens"] == 7
        assert received[0]["messages"][-1]["content"] == "Question: original question"
        assert received[0]["messages"][0]["content"] != "Changed after acceptance"
        assert (await OutboxMessage.objects.aget(run=run)).status == "completed"
        assert await RunEvent.objects.filter(run=run, event_type="node_exit").acount() == 1
        poll = await async_client.get(f"/api/v2/runs/{run_id}", headers=headers)
        assert poll.status_code == 200
        assert poll.json()["status"] == "completed"
    finally:
        await client.close()


async def test_durable_approval_continues_once_and_cancel_is_terminal(
    async_client, workflow_factory
):
    user, headers = await identity("approval")
    workflow = await workflow_factory(
        owner=user,
        nodes=[
            {"id": "approve", "kind": "hitl_approval"},
            {
                "id": "done",
                "kind": "condition",
                "config": {"condition": {"path": "enabled", "op": "exists"}},
            },
        ],
        edges=[{"source": "approve", "target": "done"}],
    )
    response = await async_client.post(
        "/api/v2/runs/execute",
        headers=headers,
        json={"workflow_id": str(workflow.id), "input_data": {"enabled": True}},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    run = await WorkflowRun.objects.aget(pk=run_id)
    assert run.status == "waiting_human"
    for _ in range(2):
        resume = await async_client.post(
            f"/api/v2/runs/{run_id}/resume",
            headers=headers,
            json={"node_id": "approve", "input": {"approved": True}},
        )
        assert resume.status_code == 202, resume.text
    assert await OutboxMessage.objects.filter(run=run, topic="resume_workflow").acount() == 1
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert (
        await RunEvent.objects.filter(
            run=run, event_type="node_exit", payload__node_id="done"
        ).acount()
        == 1
    )
    again = await async_client.post(
        f"/api/v2/runs/{run_id}/resume", headers=headers, json={"input": {}}
    )
    assert again.status_code == 400
    pending = await async_client.post(
        "/api/v2/runs/execute", headers=headers, json={"workflow_id": str(workflow.id)}
    )
    pending_id = pending.json()["run_id"]
    cancelled = await async_client.post(f"/api/v2/runs/{pending_id}/cancel", headers=headers)
    assert cancelled.status_code == 202
    await OutboxWorker.process_next_batch(executor_handler)
    assert (await WorkflowRun.objects.aget(pk=pending_id)).status == "cancelled"
