"""The browser queues the same durable owner-scoped commands as the token API."""

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from src.runs.models import OutboxMessage, RunEvent, WorkflowRun
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def console():
    owner = get_user_model().objects.create_user(username="command-owner")
    other = get_user_model().objects.create_user(username="command-other")
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    csrf = client.get("/console/api/session/").json()["csrf_token"]
    workflow = Workflow.objects.create(
        owner=owner,
        name="Approval pipeline",
        nodes=[{"id": "approval", "kind": "hitl_approval"}],
        edges=[],
    )
    return owner, other, client, csrf, workflow


def post(client, path, value, csrf, **headers):
    return client.post(
        path,
        data=json.dumps(value),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        **headers,
    )


def test_console_execute_is_durable_idempotent_and_owner_scoped(console):
    owner, other, client, csrf, workflow = console
    values = {"workflow_id": str(workflow.pk), "input_data": {"prompt": "Draft"}}
    first = post(
        client, "/console/api/execute/", values, csrf, HTTP_IDEMPOTENCY_KEY="browser-command"
    )
    assert first.status_code == 202
    second = post(
        client, "/console/api/execute/", values, csrf, HTTP_IDEMPOTENCY_KEY="browser-command"
    )
    assert second.json() == first.json()
    run = WorkflowRun.objects.get(pk=first.json()["run_id"])
    assert run.billing_owner_id == owner.pk and run.status == "pending"
    assert OutboxMessage.objects.filter(run=run).count() == 1
    changed = post(
        client,
        "/console/api/execute/",
        {**values, "input_data": {"prompt": "Different"}},
        csrf,
        HTTP_IDEMPOTENCY_KEY="browser-command",
    )
    assert changed.status_code == 409 and OutboxMessage.objects.count() == 1
    workflow.owner = other
    workflow.save()
    assert post(client, "/console/api/execute/", values, csrf).status_code == 404
    assert client.get(f"/console/api/runs/{run.pk}/").status_code == 404


def test_console_rejects_private_command_fields_and_bad_bodies(console):
    _, _, client, csrf, workflow = console
    for values in (
        {"workflow_id": "bad"},
        {"workflow_id": str(workflow.pk), "execution_token": str(uuid.uuid4())},
        {"workflow_id": str(workflow.pk), "input_data": []},
    ):
        response = post(client, "/console/api/execute/", values, csrf)
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_command"}
    assert not WorkflowRun.objects.exists()
    assert (
        post(
            client,
            "/console/api/execute/",
            {"workflow_id": str(workflow.pk)},
            csrf,
            HTTP_IDEMPOTENCY_KEY="x" * 129,
        ).status_code
        == 400
    )


def test_console_resumes_waiting_approval_and_cancels_through_existing_commands(console):
    owner, _, client, csrf, workflow = console
    run = WorkflowRun.objects.create(
        workflow=workflow,
        billing_owner=owner,
        status="waiting_human",
        checkpoint={"waiting": ["approval"]},
    )
    response = post(
        client,
        f"/console/api/runs/{run.pk}/resume/",
        {"node_id": "approval", "input": {"approved": True}},
        csrf,
    )
    assert response.status_code == 202 and response.json()["status"] == "queued"
    command = OutboxMessage.objects.get(run=run)
    assert (
        command.topic == "resume_workflow"
        and command.payload["human_input"]["node_id"] == "approval"
    )
    assert post(client, f"/console/api/runs/{run.pk}/cancel/", {}, csrf).status_code == 202
    run.refresh_from_db()
    assert run.cancel_requested
    assert (
        post(
            client, f"/console/api/runs/{run.pk}/cancel/", {"status": "completed"}, csrf
        ).status_code
        == 422
    )


def test_console_detail_exposes_waiting_nodes_without_private_checkpoint(console):
    owner, _, client, _, workflow = console
    secret = "private-execution-state"
    run = WorkflowRun.objects.create(
        workflow=workflow,
        billing_owner=owner,
        status="waiting_human",
        execution_token=uuid.uuid4(),
        checkpoint={
            "waiting": ["approval"],
            "context": {"private": secret},
            "usage": {"total_tokens": 12, "ledger_base_tokens": 100},
        },
    )
    body = client.get(f"/console/api/runs/{run.pk}/").json()
    assert body["waiting_nodes"] == ["approval"] and body["workflow_name"] == workflow.name
    assert body["usage"] == {"total_tokens": 12}
    assert "checkpoint" not in body and "execution_token" not in body
    assert secret not in json.dumps(body)


def test_console_events_are_bounded_and_have_resumable_cursor(console):
    owner, other, client, _, workflow = console
    run = WorkflowRun.objects.create(workflow=workflow, billing_owner=owner)
    for sequence in range(1, 4):
        RunEvent.objects.create(
            run=run, sequence=sequence, event_type="node_enter", payload={"node_id": "approval"}
        )
    first = client.get(f"/console/api/runs/{run.pk}/events/?limit=2").json()
    assert first["next_after"] == 2 and first["has_more"]
    assert first["events"][0]["timestamp"] and first["events"][0]["type"] == "node_enter"
    second = client.get(f"/console/api/runs/{run.pk}/events/?after=2&limit=2").json()
    assert second["next_after"] == 3 and not second["has_more"]
    assert client.get(f"/console/api/runs/{run.pk}/events/?limit=201").status_code == 400
    assert client.get(f"/console/api/runs/{run.pk}/events/?after=-1").status_code == 400
    workflow.owner = other
    workflow.save()
    assert client.get(f"/console/api/runs/{run.pk}/events/").status_code == 404


def test_console_run_filters_keep_owner_scope(console):
    owner, other, client, _, workflow = console
    run = WorkflowRun.objects.create(workflow=workflow, billing_owner=owner, status="running")
    other_workflow = Workflow.objects.create(
        owner=other,
        name="Other private",
        nodes=[{"id": "approval", "kind": "hitl_approval"}],
        edges=[],
    )
    WorkflowRun.objects.create(workflow=other_workflow, billing_owner=other, status="running")
    response = client.get("/console/api/runs/?status=running&search=Approval").json()
    assert response["count"] == 1 and response["results"][0]["id"] == str(run.pk)
    assert client.get(f"/console/api/runs/?search={run.pk}").json()["count"] == 1
    assert client.get("/console/api/runs/?status=invalid").status_code == 400
