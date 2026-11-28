"""The triage inbox exposes typed decisions and queues explicit owner approvals."""

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.authtoken.models import Token

from src.runs.models import OutboxMessage, WorkflowRun
from src.triage.models import TriageReceipt
from src.triage.schemas import CATEGORIES
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)

CLASSIFICATION = {"category": "Facturación", "reason": "La consulta trata de una factura."}
CONFIRMATION = {"approved": True, "category": "Acceso", "note": "Revisado por una persona."}
BASE = "/console/api/triage/runs/"


@pytest.fixture
def console(settings):
    settings.DEMO_TRIAGE_ENABLED = True
    owner = get_user_model().objects.create_user(username="triage-owner")
    other = get_user_model().objects.create_user(username="triage-other")
    workflow = Workflow.objects.create(
        owner=owner,
        name="Spanish ticket triage",
        nodes=[{"id": "approval", "kind": "hitl_approval"}],
        edges=[],
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    csrf = client.get("/console/api/session/").json()["csrf_token"]
    return SimpleNamespace(owner=owner, other=other, workflow=workflow, client=client, csrf=csrf)


def make_run(console, **overrides):
    values = {
        "workflow": console.workflow,
        "billing_owner": console.owner,
        "status": "waiting_human",
        "input_data": {
            "ticket_id": "TK-42",
            "subject": "Factura",
            "text": "Me cobraron dos veces.",
        },
        "output_schema_snapshots": {"classify": {"id": "ticket_classification", "version": 1}},
        "checkpoint": {
            "completed": ["classify"],
            "waiting": ["approval"],
            "context": {"nodes": {"classify": {"result": CLASSIFICATION}}},
        },
    }
    values.update(overrides)
    return WorkflowRun.objects.create(**values)


def receipt_for(run, **overrides):
    values = {
        "idempotency_key": f"{run.pk}:save_ticket:tool",
        "run_id": run.pk,
        "workflow_id": run.workflow_id,
        "node_id": "save_ticket",
        "ticket_id": run.input_data["ticket_id"],
        "category": "Acceso",
        "payload": {"private": "private-destination-payload"},
        "payload_hash": "a" * 64,
    }
    values.update(overrides)
    return TriageReceipt.objects.create(**values)


def confirm(console, run, value=None, csrf=None, **headers):
    return console.client.post(
        f"{BASE}{run.pk}/confirm/",
        data=json.dumps(CONFIRMATION if value is None else value),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=console.csrf if csrf is None else csrf,
        **headers,
    )


def test_inbox_exposes_only_typed_public_fields_and_no_private_state(console):
    secret = "private-checkpoint-or-provider-value"
    run = make_run(console, execution_token=uuid.uuid4())
    run.checkpoint["context"]["private"] = secret
    run.output_data = {"private": secret}
    run.save()
    response = console.client.get(BASE)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    body = response.json()
    assert body["enabled"] is True and body["categories"] == list(CATEGORIES)
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert set(row) == {
        "id",
        "status",
        "ticket_id",
        "text",
        "classification",
        "confirmation",
        "waiting_for_approval",
        "created_at",
        "updated_at",
        "receipt",
    }
    assert row["id"] == str(run.pk) and row["ticket_id"] == "TK-42"
    assert row["text"] == "Me cobraron dos veces."
    assert row["classification"] == CLASSIFICATION and row["confirmation"] is None
    assert row["waiting_for_approval"] is True and row["receipt"] is None
    assert secret not in response.content.decode()
    assert "checkpoint" not in row and "execution_token" not in row
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()


@pytest.mark.parametrize(
    "checkpoint",
    [
        [],
        {"completed": "classify", "context": {"nodes": {"classify": {"result": CLASSIFICATION}}}},
        {"completed": ["classify"], "context": []},
        {"completed": ["classify"], "context": {"nodes": []}},
        {
            "completed": ["classify"],
            "context": {"nodes": {"classify": {"result": {**CLASSIFICATION, "private": "secret"}}}},
        },
    ],
)
def test_inbox_handles_missing_or_unexpected_checkpoint_shapes_without_exposing_them(
    console, checkpoint
):
    make_run(console, checkpoint=checkpoint, input_data=[])
    response = console.client.get(BASE)
    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["ticket_id"] == row["text"] == ""
    assert row["classification"] is row["confirmation"] is None
    assert row["waiting_for_approval"] is False
    assert "secret" not in response.content.decode()


@pytest.mark.parametrize("staff", [False, True])
def test_inbox_and_confirmation_remain_strictly_owner_scoped_even_for_staff(console, staff):
    console.owner.is_staff = staff
    console.owner.save(update_fields=["is_staff"])
    own = make_run(console)
    foreign_workflow = Workflow.objects.create(
        owner=console.other,
        name="Foreign triage",
        nodes=console.workflow.nodes,
        edges=console.workflow.edges,
    )
    foreign = make_run(console, workflow=foreign_workflow, billing_owner=console.other)
    receipt_for(foreign)
    body = console.client.get(BASE).json()
    assert [row["id"] for row in body["results"]] == [str(own.pk)]
    assert body["results"][0]["receipt"] is None
    assert confirm(console, foreign).status_code == 404
    assert not OutboxMessage.objects.exists()


def test_inbox_uses_frozen_schema_identity_and_a_fifty_run_bound(console):
    first = make_run(console)
    for _ in range(50):
        latest = make_run(console)
    make_run(console, output_schema_snapshots={})
    make_run(console, output_schema_snapshots={"classify": {"id": "review_decision", "version": 1}})
    make_run(
        console, output_schema_snapshots={"classify": {"id": "ticket_classification", "version": 2}}
    )
    console.workflow.name = "Renamed after this run was created"
    console.workflow.save(update_fields=["name"])
    rows = console.client.get(BASE).json()["results"]
    assert len(rows) == 50 and rows[0]["id"] == str(latest.pk)
    assert str(first.pk) not in {row["id"] for row in rows}


def test_confirm_queues_the_explicit_approval_with_corrected_category_and_no_destination_write(
    console,
):
    run = make_run(console)
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()
    response = confirm(console, run, HTTP_IDEMPOTENCY_KEY="confirm-42")
    assert response.status_code == 202
    assert response.json() == {"run_id": str(run.pk), "status": "queued"}
    command = OutboxMessage.objects.get(run=run)
    assert command.topic == "resume_workflow"
    assert command.payload["human_input"] == {"node_id": "approval", "input": CONFIRMATION}
    assert not TriageReceipt.objects.exists()
    row = console.client.get(BASE).json()["results"][0]
    assert row["classification"] == CLASSIFICATION and row["confirmation"] == CONFIRMATION
    assert row["waiting_for_approval"] is False and row["receipt"] is None


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_pending_approval_commands_hide_the_approval_action(console, status):
    run = make_run(console)
    OutboxMessage.objects.create(
        run=run,
        topic="resume_workflow",
        status=status,
        payload={"human_input": {"node_id": "approval", "input": CONFIRMATION}},
    )
    row = console.client.get(BASE).json()["results"][0]
    assert row["waiting_for_approval"] is False and row["confirmation"] == CONFIRMATION


def test_saved_confirmation_and_receipt_are_visible_without_destination_payload(console):
    run = make_run(console, status="completed")
    run.checkpoint["completed"].extend(["approval", "approved", "save_ticket"])
    run.checkpoint["waiting"] = []
    run.checkpoint["context"]["nodes"]["approval"] = {"result": CONFIRMATION}
    run.save()
    receipt = receipt_for(run)
    response = console.client.get(BASE)
    row = response.json()["results"][0]
    assert row["confirmation"] == CONFIRMATION and row["receipt"] == receipt.as_receipt()
    assert row["waiting_for_approval"] is False
    assert "private-destination-payload" not in response.content.decode()


@pytest.mark.parametrize(
    "value",
    [
        {"approved": False, "category": "Acceso"},
        {"approved": "true", "category": "Acceso"},
        {"approved": 1, "category": "Acceso"},
        {"category": "Acceso"},
        {"approved": True, "category": "Billing"},
        {"approved": True, "category": "Acceso", "note": "x" * 1001},
        {"approved": True, "category": "Acceso", "execution_token": "forged"},
        {"approved": True, "category": "Acceso", "node_id": "save_ticket"},
    ],
)
def test_confirmation_requires_strict_positive_approval_and_the_exact_contract(console, value):
    response = confirm(console, make_run(console), value)
    assert response.status_code == 422 and response.json() == {"code": "invalid_command"}
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()


@pytest.mark.parametrize(
    "completed,classification", [([], CLASSIFICATION), (["classify"], {"category": "Acceso"})]
)
def test_confirmation_requires_a_completed_valid_classification(console, completed, classification):
    run = make_run(
        console,
        checkpoint={
            "completed": completed,
            "waiting": ["approval"],
            "context": {"nodes": {"classify": {"result": classification}}},
        },
    )
    assert confirm(console, run).status_code == 409
    row = console.client.get(BASE).json()["results"][0]
    assert row["classification"] is None and row["waiting_for_approval"] is False
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()


def test_confirmation_retries_stay_idempotent_after_completion_and_reject_changed_data(console):
    run = make_run(console)
    first = confirm(console, run, HTTP_IDEMPOTENCY_KEY="durable-confirmation")
    assert first.status_code == 202
    run.status = "completed"
    run.checkpoint["waiting"] = []
    run.checkpoint["context"]["nodes"]["approval"] = {"result": CONFIRMATION}
    run.save()
    OutboxMessage.objects.filter(run=run).update(status="completed")
    retry = confirm(console, run, HTTP_IDEMPOTENCY_KEY="durable-confirmation")
    assert retry.status_code == 202 and retry.json() == first.json()
    changed = confirm(
        console,
        run,
        {**CONFIRMATION, "category": "Otros"},
        HTTP_IDEMPOTENCY_KEY="durable-confirmation",
    )
    assert changed.status_code == 409 and changed.json() == {"code": "command_rejected"}
    assert OutboxMessage.objects.filter(run=run).count() == 1
    assert not TriageReceipt.objects.exists()


def test_confirmation_rejects_nontriage_runs_and_invalid_idempotency_keys(console):
    ordinary = make_run(console, output_schema_snapshots={})
    assert confirm(console, ordinary).status_code == 404
    run = make_run(console)
    response = confirm(console, run, HTTP_IDEMPOTENCY_KEY="x" * 129)
    assert response.status_code == 400 and response.json() == {"code": "invalid_idempotency_key"}
    assert not OutboxMessage.objects.exists()


def test_triage_uses_existing_session_and_csrf_guards_without_token_fallback(console):
    run = make_run(console)
    anonymous = Client(enforce_csrf_checks=True)
    assert anonymous.get(BASE).status_code == 401
    token = Token.objects.create(user=console.owner)
    assert anonymous.get(BASE, HTTP_AUTHORIZATION=f"Token {token.key}").status_code == 401
    assert confirm(console, run, csrf="").status_code == 403
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()


def test_disabled_demo_has_an_empty_inbox_and_no_confirmation_route(console, settings):
    run = make_run(console)
    settings.DEMO_TRIAGE_ENABLED = False
    response = console.client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "categories": list(CATEGORIES), "results": []}
    assert confirm(console, run).status_code == 404
    assert not OutboxMessage.objects.exists() and not TriageReceipt.objects.exists()
