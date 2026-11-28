"""The demo destination deduplicates writes and saves only recorded human choices."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
import respx
from asgiref.sync import sync_to_async
from django.db import IntegrityError, close_old_connections, connection, transaction
from pydantic import ValidationError

from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolArgumentsValidationError, ToolRegistry
from src.runs.models import WorkflowRun
from src.triage.models import TriageReceipt, TriageSinkAttempt
from src.triage.schemas import CATEGORIES, ConfirmedCategoryV1, SinkSubmissionV1
from src.triage.services import accept_submission
from src.triage.tools import configured_sink_url
from src.workflows.models import Workflow
from src.workflows.output_schemas import OutputSchemaRegistry


def submission(**changes):
    run_id = uuid4()
    values = {
        "run_id": run_id,
        "workflow_id": uuid4(),
        "node_id": "save_ticket",
        "idempotency_key": f"{run_id}:save_ticket:tool",
        "ticket_id": "ES-001",
        "classification": {"category": "Otros", "reason": "Revisar el mensaje."},
        "confirmation": {"approved": True, "category": "Facturación", "note": "Cargo duplicado."},
    }
    values.update(changes)
    return SinkSubmissionV1.model_validate(values)


def test_classification_contract_is_registered_and_fixtures_are_separate():
    schema = OutputSchemaRegistry.get({"id": "ticket_classification", "version": 1})
    assert set(schema.json_schema["properties"]) == {"category", "reason"}
    assert schema.json_schema["$defs"]["TicketCategory"]["enum"] == list(CATEGORIES)
    data = Path(__file__).parents[2] / "src/triage/data"
    tickets = json.loads((data / "tickets_es.json").read_text())
    expected = json.loads((data / "expected_es.json").read_text())
    assert 5 <= len(tickets) <= 10
    assert all(set(ticket) == {"ticket_id", "subject", "text"} for ticket in tickets)
    assert {ticket["ticket_id"] for ticket in tickets} == set(expected)
    assert set(expected.values()) == set(CATEGORIES)


@pytest.mark.parametrize("approved", [False, 1, 0, "true", "false", "yes", None, [], {}])
def test_confirmation_requires_boolean_true(approved):
    with pytest.raises(ValidationError):
        ConfirmedCategoryV1.model_validate({"approved": approved, "category": "Facturación"})


@pytest.mark.parametrize("category", ["billing", "facturación", "Facturacion", "Acceso ", 1, None])
def test_confirmation_requires_exact_category(category):
    with pytest.raises(ValidationError):
        ConfirmedCategoryV1.model_validate({"approved": True, "category": category})


def test_model_arguments_cannot_supply_trusted_save_context():
    tool = next(
        spec for spec in ToolRegistry.get_specs() if spec.name == "save_ticket_classification"
    )
    assert set(tool.parameters_schema["properties"]) == {
        "ticket_id",
        "classification",
        "confirmation",
    }
    args = submission().model_dump(mode="json")
    with pytest.raises(ToolArgumentsValidationError):
        ToolRegistry.validate_arguments(tool.name, args, allowed_tool_ids=[tool.id])


@pytest.mark.django_db
def test_destination_same_key_returns_original_receipt_and_records_attempts():
    item = submission()
    first = accept_submission(item)
    replay = accept_submission(item)
    assert first.outcome == "created"
    assert replay.outcome == "replayed"
    assert replay.receipt.as_receipt() == first.receipt.as_receipt()
    assert TriageReceipt.objects.count() == 1
    assert first.receipt.category == "Facturación"
    assert first.receipt.payload["classification"]["category"] == "Otros"
    assert first.receipt.payload["confirmation"]["category"] == "Facturación"
    assert list(TriageSinkAttempt.objects.values_list("outcome", flat=True)) == [
        "created",
        "replayed",
    ]
    with pytest.raises(IntegrityError), transaction.atomic():
        TriageReceipt.objects.create(
            idempotency_key=item.idempotency_key,
            run_id=item.run_id,
            workflow_id=item.workflow_id,
            node_id=item.node_id,
            ticket_id=item.ticket_id,
            payload={},
            payload_hash="0" * 64,
            category="Otros",
        )


@pytest.mark.django_db
def test_destination_rejects_changed_payload_without_modifying_original():
    item = submission()
    first = accept_submission(item)
    changed = item.model_dump()
    changed["confirmation"]["category"] = "Acceso"
    result = accept_submission(SinkSubmissionV1.model_validate(changed))
    assert result.outcome == "conflict"
    assert result.receipt.pk == first.receipt.pk
    assert result.receipt.category == "Facturación"
    assert TriageReceipt.objects.count() == 1
    assert list(TriageSinkAttempt.objects.values_list("outcome", flat=True)) == [
        "created",
        "conflict",
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.parametrize("conflicting", [False, True])
def test_database_unique_key_serializes_concurrent_destination_writes(conflicting):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL concurrent unique-key semantics")
    item = submission()
    start = Barrier(6)

    def write(index):
        close_old_connections()
        try:
            value = item.model_dump()
            if conflicting and index % 2:
                value["confirmation"]["category"] = "Acceso"
            start.wait(timeout=10)
            result = accept_submission(SinkSubmissionV1.model_validate(value))
            return result.outcome, str(result.receipt.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(write, range(6)))
    assert TriageReceipt.objects.count() == 1
    assert len({receipt_id for _, receipt_id in results}) == 1
    outcomes = [outcome for outcome, _ in results]
    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == (3 if conflicting else 0)
    assert TriageSinkAttempt.objects.count() == 6


@pytest.mark.django_db(transaction=True)
def test_sink_commits_before_fault_barrier_and_replay_skips_it(client, settings, monkeypatch):
    settings.DEMO_TRIAGE_ENABLED = True
    settings.DEMO_TRIAGE_SINK_TOKEN = "test-only-sink-token"
    item = submission()
    committed = []

    def barrier(run_id):
        assert not connection.in_atomic_block
        committed.append(TriageReceipt.objects.get(run_id=run_id).as_receipt())

    monkeypatch.setattr("src.triage.views._commit_barrier", barrier)
    params = {
        "data": item.model_dump_json(),
        "content_type": "application/json",
        "headers": {
            "X-Demo-Triage-Token": settings.DEMO_TRIAGE_SINK_TOKEN,
            "Idempotency-Key": item.idempotency_key,
        },
    }
    first = client.post("/api/demo/triage/sink/", **params)
    replay = client.post("/api/demo/triage/sink/", **params)
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json() == committed[0]
    assert len(committed) == 1
    changed = item.model_dump(mode="json")
    changed["classification"]["reason"] = "Different recommendation, same key."
    params["data"] = json.dumps(changed)
    assert client.post("/api/demo/triage/sink/", **params).status_code == 409
    assert list(TriageSinkAttempt.objects.values_list("outcome", flat=True)) == [
        "created",
        "replayed",
        "conflict",
    ]


@pytest.mark.django_db
def test_sink_requires_opt_in_secret_and_matching_key(client, settings):
    item = submission()
    settings.DEMO_TRIAGE_ENABLED = False
    assert client.post("/api/demo/triage/sink/").status_code == 404
    settings.DEMO_TRIAGE_ENABLED = True
    settings.DEMO_TRIAGE_SINK_TOKEN = "test-only-sink-token"
    assert client.post("/api/demo/triage/sink/").status_code == 403
    assert (
        client.post(
            "/api/demo/triage/sink/",
            data=item.model_dump_json(),
            content_type="application/json",
            headers={
                "X-Demo-Triage-Token": settings.DEMO_TRIAGE_SINK_TOKEN,
                "Idempotency-Key": "wrong",
            },
        ).status_code
        == 400
    )
    assert TriageReceipt.objects.count() == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/api/demo/triage/sink/",
        "http://169.254.169.254/api/demo/triage/sink/",
        "http://localhost:8020/other/",
        "http://user:pass@localhost/api/demo/triage/sink/",
        "http://localhost/api/demo/triage/sink/?url=http://example.com",
        "file:///api/demo/triage/sink/",
    ],
)
def test_trusted_destination_rejects_nonlocal_or_repurposed_endpoints(settings, endpoint):
    settings.DEMO_TRIAGE_SINK_URL = endpoint
    with pytest.raises(ValueError):
        configured_sink_url()


@pytest.fixture
def recorded_run(transactional_db, django_user_model):
    owner = django_user_model.objects.create_user(username="triage-owner")
    tool = next(
        spec for spec in ToolRegistry.get_specs() if spec.name == "save_ticket_classification"
    )
    agent_id = uuid4()
    workflow = Workflow.objects.create(
        owner=owner,
        name="Ticket triage test",
        nodes=[
            {
                "id": "classify",
                "kind": "llm_call",
                "agent_id": str(agent_id),
                "output_schema": {"id": "ticket_classification", "version": 1},
            },
            {"id": "approval", "kind": "hitl_approval"},
            {"id": "save_ticket", "kind": "tool_exec", "tool_id": str(tool.id)},
        ],
        edges=[
            {"source": "classify", "target": "approval"},
            {"source": "approval", "target": "save_ticket"},
        ],
    )
    item = submission(workflow_id=workflow.pk)
    run = WorkflowRun.objects.create(
        id=item.run_id,
        workflow=workflow,
        billing_owner=owner,
        status="running",
        input_data={"ticket_id": item.ticket_id},
        workflow_snapshot=workflow.to_spec().model_dump(mode="json"),
        checkpoint={
            "completed": ["classify", "approval"],
            "human_inputs": {"approval": item.confirmation.model_dump()},
            "node_states": {"classify": {"done": True, "result": item.classification.model_dump()}},
        },
    )
    context = ToolExecutionContext(
        run_id=run.pk,
        workflow_id=workflow.pk,
        node_id=item.node_id,
        tool_call_id="tool",
        owner_id=owner.pk,
        idempotency_key=item.idempotency_key,
    )
    return item, run, context, tool


async def test_save_tool_forwards_only_recorded_confirmation_and_trusted_identity(
    settings, recorded_run
):
    item, run, context, tool = recorded_run
    settings.DEMO_TRIAGE_ENABLED = True
    settings.DEMO_TRIAGE_SINK_URL = "http://127.0.0.1:8021/api/demo/triage/sink/"
    settings.DEMO_TRIAGE_SINK_TOKEN = "test-only-sink-token"
    receipt = {
        "receipt_id": str(uuid4()),
        "run_id": str(run.pk),
        "ticket_id": item.ticket_id,
        "category": "Facturación",
        "idempotency_key": item.idempotency_key,
    }
    with respx.mock as router:
        route = router.post(settings.DEMO_TRIAGE_SINK_URL).mock(
            return_value=httpx.Response(201, json=receipt)
        )
        result = await ToolRegistry.execute(
            tool.name,
            {
                key: item.model_dump(mode="json")[key]
                for key in ("ticket_id", "classification", "confirmation")
            },
            allowed_tool_ids=[tool.id],
            execution_context=context,
        )
    assert result == receipt
    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == item.idempotency_key
    assert request.headers["X-Demo-Triage-Token"] == settings.DEMO_TRIAGE_SINK_TOKEN
    assert json.loads(request.content) == item.model_dump(mode="json")


@pytest.mark.parametrize(
    "corruption", ["no_human", "wrong_owner", "wrong_ticket", "wrong_recommendation"]
)
async def test_save_tool_refuses_unrecorded_or_mismatched_context(
    settings, recorded_run, corruption
):
    item, run, context, tool = recorded_run
    settings.DEMO_TRIAGE_ENABLED = True
    if corruption == "no_human":
        run.checkpoint["human_inputs"] = {}
    elif corruption == "wrong_owner":
        run.billing_owner_id = None
    elif corruption == "wrong_ticket":
        run.input_data["ticket_id"] = "ES-999"
    elif corruption == "wrong_recommendation":
        run.checkpoint = copy.deepcopy(run.checkpoint)
        run.checkpoint["node_states"]["classify"]["result"]["category"] = "Acceso"
    await sync_to_async(run.save, thread_sensitive=True)()
    with respx.mock as router, pytest.raises(ValueError):
        await ToolRegistry.execute(
            tool.name,
            {
                key: item.model_dump(mode="json")[key]
                for key in ("ticket_id", "classification", "confirmation")
            },
            allowed_tool_ids=[tool.id],
            execution_context=context,
        )
    assert not router.calls
