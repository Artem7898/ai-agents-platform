"""Exercise real routing, token authentication, scoping and command boundaries."""

import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

from src.agents.models import Agent
from src.runs.models import RunEvent, WorkflowRun
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def identities():
    users = {}
    for name in ("alice", "bob", "staff"):
        user = get_user_model().objects.create_user(
            username=f"{name}-{uuid.uuid4().hex}",
            password="test-only",
            is_staff=name == "staff",
        )
        token = Token.objects.create(user=user)
        users[name] = (user, {"Authorization": f"Token {token.key}"})
    return users


def make_workflow(owner, **kwargs):
    return Workflow.objects.create(
        owner=owner,
        name=f"access-{uuid.uuid4().hex}",
        nodes=[
            {
                "id": "condition",
                "kind": "condition",
                "config": {"condition": {"path": "input.enabled", "op": "truthy"}},
            }
        ],
        edges=[],
        **kwargs,
    )


async def make_run(owner, status="pending"):
    workflow = await sync_to_async(make_workflow)(owner)
    return await WorkflowRun.objects.acreate(
        workflow=workflow,
        status=status,
        input_data={"private": "owner only"},
        workflow_snapshot=workflow.to_spec().model_dump(mode="json"),
        checkpoint={"waiting": ["condition"]} if status == "waiting_human" else {},
    )


async def test_both_api_stacks_reject_anonymous_and_cookie_only_requests(async_client):
    run_id = uuid.uuid4()
    endpoints = [
        ("GET", "/api/v1/agents/", None),
        ("GET", f"/api/v2/runs/{run_id}", None),
        ("GET", f"/api/v2/runs/{run_id}/events", None),
        ("POST", "/api/v2/runs/execute", {"workflow_id": str(uuid.uuid4())}),
        ("POST", f"/api/v2/runs/{run_id}/resume", {"input": {}}),
        ("POST", f"/api/v2/runs/{run_id}/cancel", {}),
    ]
    for method, url, body in endpoints:
        response = await async_client.request(method, url, json=body)
        assert response.status_code == 401, (url, response.text)
        assert response.headers["www-authenticate"] == "Token"


async def test_inactive_or_invalid_token_is_rejected(async_client, identities):
    alice, headers = identities["alice"]
    await get_user_model().objects.filter(pk=alice.pk).aupdate(is_active=False)
    response = await async_client.get("/api/v1/agents/", headers=headers)
    assert response.status_code == 401
    for authorization in ("Token nonexistent", "Bearer nonexistent", "Token a b"):
        response = await async_client.get(
            f"/api/v2/runs/{uuid.uuid4()}/events", headers={"Authorization": authorization}
        )
        assert response.status_code == 401


async def test_owner_scoping_and_read_only_runs(async_client, identities):
    alice, headers = identities["alice"]
    bob, bob_headers = identities["bob"]
    run = await make_run(alice)
    own = await async_client.get(f"/api/v2/runs/{run.pk}", headers=headers)
    assert own.status_code == 200
    assert own.json()["input_data"] == {"private": "owner only"}
    for url in (
        f"/api/v2/runs/{run.pk}",
        f"/api/v2/runs/{run.pk}/events",
        f"/api/v1/runs/{run.pk}/",
    ):
        response = await async_client.get(url, headers=bob_headers)
        assert response.status_code == 404
    response = await async_client.get("/api/v1/workflows/", headers=bob_headers)
    assert response.status_code == 200
    assert response.json()["results"] == []
    for method in ("PATCH", "DELETE"):
        response = await async_client.request(
            method, f"/api/v1/runs/{run.pk}/", headers=headers, json={"status": "completed"}
        )
        assert response.status_code == 405
    response = await async_client.get(f"/api/v2/runs/{run.pk}", headers=identities["staff"][1])
    assert response.status_code == 200


async def test_definition_owner_cannot_be_spoofed(async_client, identities):
    alice, headers = identities["alice"]
    bob, bob_headers = identities["bob"]
    response = await async_client.post(
        "/api/v1/agents/",
        headers=headers,
        json={
            "name": "Owner check",
            "model_name": "test",
            "system_prompt": "Test prompt",
            "tool_ids": [],
            "owner": bob.pk,
        },
    )
    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]
    assert response.json()["owner"] == alice.pk
    assert await Agent.objects.filter(pk=agent_id, owner=alice).aexists()
    response = await async_client.get(f"/api/v1/agents/{agent_id}/", headers=bob_headers)
    assert response.status_code == 404
    response = await async_client.patch(
        f"/api/v1/agents/{agent_id}/", headers=headers, json={"owner": bob.pk}
    )
    assert response.status_code == 200
    assert response.json()["owner"] == alice.pk


async def test_workflow_cannot_reference_another_owners_agent(async_client, identities):
    bob, _ = identities["bob"]
    agent = await Agent.objects.acreate(
        owner=bob, name="Private", model_name="test", system_prompt="Test prompt", tool_ids=[]
    )
    response = await async_client.post(
        "/api/v1/workflows/",
        headers=identities["alice"][1],
        json={
            "name": "Invalid scope",
            "nodes": [{"id": "llm", "kind": "llm_call", "agent_id": str(agent.pk)}],
            "edges": [],
        },
    )
    assert response.status_code == 400, response.text


async def test_invalid_workflow_graph_rejected_before_save(async_client, identities):
    response = await async_client.post(
        "/api/v1/workflows/",
        headers=identities["alice"][1],
        json={
            "name": "Cycle",
            "nodes": [
                {"id": "a", "kind": "hitl_approval"},
                {"id": "b", "kind": "hitl_approval"},
            ],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
        },
    )
    assert response.status_code == 400
    assert not await Workflow.objects.filter(name="Cycle").aexists()


async def test_execute_body_types_and_missing_workflow(async_client, identities):
    headers = identities["alice"][1]
    for bad in (123, [1], {"id": "x"}, "not-a-uuid"):
        response = await async_client.post(
            "/api/v2/runs/execute", headers=headers, json={"workflow_id": bad}
        )
        assert response.status_code == 422, response.text
    response = await async_client.post(
        "/api/v2/runs/execute", headers=headers, json={"workflow_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


async def test_run_event_pagination_is_bounded_and_owner_scoped(async_client, identities):
    run = await make_run(identities["alice"][0])
    for sequence in range(1, 4):
        await RunEvent.objects.acreate(
            run=run,
            sequence=sequence,
            event_type="test",
            payload={"index": sequence},
            context_snapshot={"private": "owner only"},
        )
    url = f"/api/v2/runs/{run.pk}/events"
    response = await async_client.get(url + "?limit=2", headers=identities["alice"][1])
    assert response.status_code == 200, response.text
    page = response.json()
    assert [event["sequence"] for event in page["events"]] == [1, 2]
    assert page["has_more"] is True
    assert page["next_after"] == 2
    response = await async_client.get(url + "?after=2&limit=2", headers=identities["alice"][1])
    assert [event["sequence"] for event in response.json()["events"]] == [3]
    assert response.json()["has_more"] is False
    for query in ("?limit=201", "?limit=0", "?after=-1"):
        response = await async_client.get(url + query, headers=identities["alice"][1])
        assert response.status_code == 422
    response = await async_client.get(url, headers=identities["bob"][1])
    assert response.status_code == 404


async def test_execute_is_durable_and_idempotent(async_client, identities):
    from src.runs.models import OutboxMessage

    alice, headers = identities["alice"]
    workflow = await sync_to_async(make_workflow)(alice)
    headers = {**headers, "Idempotency-Key": "one-command"}
    body = {"workflow_id": str(workflow.pk), "input_data": {"enabled": True}}
    first = await async_client.post("/api/v2/runs/execute", headers=headers, json=body)
    second = await async_client.post("/api/v2/runs/execute", headers=headers, json=body)
    assert first.status_code == second.status_code == 202, (first.text, second.text)
    assert first.json()["run_id"] == second.json()["run_id"]
    assert await WorkflowRun.objects.filter(workflow=workflow).acount() == 1
    assert await OutboxMessage.objects.filter(run_id=first.json()["run_id"]).acount() == 1
    forbidden = await async_client.post(
        "/api/v2/runs/execute", headers=identities["bob"][1], json=body
    )
    assert forbidden.status_code == 404


async def test_resume_only_enqueues_owned_waiting_runs(async_client, identities):
    from src.runs.models import OutboxMessage
    from src.runs.schemas import RunCheckpoint

    alice, headers = identities["alice"]
    workflow = await Workflow.objects.acreate(
        owner=alice,
        name="Approval",
        nodes=[{"id": "approval", "kind": "hitl_approval"}],
        edges=[],
    )
    run = await WorkflowRun.objects.acreate(
        workflow=workflow,
        status="waiting_human",
        input_data={},
        workflow_snapshot=workflow.to_spec().model_dump(mode="json"),
        checkpoint=RunCheckpoint(waiting=["approval"]).model_dump(mode="json"),
    )
    body = {"input": {"approved": True}, "node_id": "approval"}
    url = f"/api/v2/runs/{run.pk}/resume"
    forbidden = await async_client.post(url, headers=identities["bob"][1], json=body)
    assert forbidden.status_code == 404
    response = await async_client.post(url, headers=headers, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    await run.arefresh_from_db()
    assert run.status == "waiting_human"
    assert run.checkpoint["waiting"] == ["approval"]
    command = await OutboxMessage.objects.aget(run=run)
    assert command.topic == "resume_workflow"
    assert command.payload["human_input"] == body
    repeated = await async_client.post(url, headers=headers, json=body)
    assert repeated.status_code == 202
    assert await OutboxMessage.objects.filter(run=run).acount() == 1
    conflict = await async_client.post(
        url, headers=headers, json={"input": {"approved": False}, "node_id": "approval"}
    )
    assert conflict.status_code == 400


async def test_cancel_checks_owner_and_terminal_state(async_client, identities):
    run = await make_run(identities["alice"][0])
    url = f"/api/v2/runs/{run.pk}/cancel"
    forbidden = await async_client.post(url, headers=identities["bob"][1])
    assert forbidden.status_code == 404
    accepted = await async_client.post(url, headers=identities["alice"][1])
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "cancelled"
    await run.arefresh_from_db()
    assert run.cancel_requested is True
    completed = await make_run(identities["alice"][0], status="completed")
    response = await async_client.post(
        f"/api/v2/runs/{completed.pk}/cancel", headers=identities["alice"][1]
    )
    assert response.status_code == 400


async def test_definitions_with_runs_cannot_be_deleted(async_client, identities):
    run = await make_run(identities["alice"][0])
    response = await async_client.delete(
        f"/api/v1/workflows/{run.workflow_id}/", headers=identities["alice"][1]
    )
    assert response.status_code == 409
    assert await Workflow.objects.filter(pk=run.workflow_id).aexists()


async def test_workflow_definition_round_trip_preserves_limits_and_output(async_client, identities):
    alice, headers = identities["alice"]
    response = await async_client.post(
        "/api/v1/workflows/",
        headers=headers,
        json={
            "name": "Bounded condition",
            "version": 1,
            "nodes": [
                {
                    "id": "decide",
                    "kind": "condition",
                    "config": {"condition": {"path": "input.enabled", "op": "truthy"}},
                }
            ],
            "edges": [],
            "limits": {"max_steps": 8},
            "output_node_id": "decide",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["owner"] == alice.pk
    assert data["limits"]["max_steps"] == 8
    assert data["output_node_id"] == "decide"
    workflow = await Workflow.objects.aget(pk=data["id"])
    assert workflow.to_spec().output_node_id == "decide"
    assert workflow.to_spec().limits.max_steps == 8


async def test_operations_requires_staff_before_collecting_metrics(async_client, identities):
    from unittest.mock import AsyncMock, patch

    with patch("src.api.operations.collect_operations", new_callable=AsyncMock) as collect:
        anonymous = await async_client.get("/api/v2/operations")
        ordinary = await async_client.get("/api/v2/operations", headers=identities["alice"][1])
    assert anonymous.status_code == 401
    assert ordinary.status_code == 403
    collect.assert_not_awaited()


async def test_operations_counts_lag_expired_leases_and_recorded_tokens(async_client, identities):
    from datetime import timedelta

    from django.utils import timezone

    from src.runs.models import OutboxMessage

    now = timezone.now()
    run = await make_run(identities["alice"][0])
    completed = await make_run(identities["alice"][0], status="completed")
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(checkpoint={"usage": {"total_tokens": 7}})
    await WorkflowRun.objects.filter(pk=completed.pk).aupdate(
        checkpoint={"usage": {"total_tokens": 11}}
    )
    pending = await OutboxMessage.objects.acreate(run=run, topic="run_workflow")
    await OutboxMessage.objects.filter(pk=pending.pk).aupdate(
        created_at=now - timedelta(seconds=90)
    )
    await OutboxMessage.objects.acreate(
        run=run,
        topic="run_workflow",
        status="processing",
        lease_expires_at=now - timedelta(seconds=5),
    )
    await OutboxMessage.objects.acreate(
        run=run,
        topic="run_workflow",
        status="processing",
        lease_expires_at=now + timedelta(minutes=5),
    )
    response = await async_client.get("/api/v2/operations", headers=identities["staff"][1])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["run_counts"]["pending"] == 1
    assert data["run_counts"]["completed"] == 1
    assert data["command_counts"]["pending"] == 1
    assert data["command_counts"]["processing"] == 2
    assert 90 <= data["oldest_pending_age_seconds"] < 150
    assert data["expired_processing_count"] == 1
    assert data["total_tokens"] == 18
    assert set(data) == {
        "collected_at",
        "run_counts",
        "command_counts",
        "oldest_pending_age_seconds",
        "expired_processing_count",
        "total_tokens",
    }
