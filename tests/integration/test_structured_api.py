"""Structured-output contracts are discovered and frozen through real API boundaries."""

from copy import deepcopy

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from pydantic import BaseModel, ConfigDict, StrictBool
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from src.agents.models import Agent
from src.runs.models import OutboxMessage, WorkflowRun
from src.runs.services import create_run
from src.workflows.models import Workflow
from src.workflows.output_schemas import OutputSchemaError, OutputSchemaRegistry

pytestmark = pytest.mark.django_db(transaction=True)


class ReviewedV2(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    approved: StrictBool
    reason: str


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(
        OutputSchemaRegistry, "_definitions", dict(OutputSchemaRegistry._definitions)
    )


@pytest.fixture
def identity(catalog):
    owner = get_user_model().objects.create_user(username="structured-api-owner")
    token = Token.objects.create(user=owner)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    agent = Agent.objects.create(
        owner=owner,
        name="Structured agent",
        model_name="model",
        system_prompt="Review the input.",
        status="active",
    )
    return owner, client, agent


def workflow_body(agent, *, reference=None, **kwargs):
    reference = reference if reference is not None else {"id": "review_decision", "version": 1}
    return {
        "name": "Structured decision",
        "nodes": [
            {
                "id": "review",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
                "output_schema": reference,
            }
        ],
        "edges": [],
        "output_node_id": "review",
        **kwargs,
    }


def create_workflow(client, agent, **kwargs):
    response = client.post("/api/v1/workflows/", workflow_body(agent, **kwargs), format="json")
    assert response.status_code == 201, response.data
    return Workflow.objects.get(pk=response.data["id"])


def test_schema_catalog_requires_token_and_returns_only_public_contract_metadata(identity):
    owner, client, _ = identity
    assert APIClient().get("/api/v1/output-schemas/").status_code == 401
    response = client.get("/api/v1/output-schemas/")
    assert response.status_code == 200
    assert response.data["count"] >= 1
    definition = next(row for row in response.data["results"] if row["id"] == "review_decision")
    assert set(definition) == {"id", "version", "description", "json_schema", "fingerprint"}
    assert definition["version"] == 1
    assert set(definition["json_schema"]["properties"]) == {"approved", "score"}
    assert len(definition["fingerprint"]) == 64
    for is_staff in (False, True):
        owner.is_staff = is_staff
        owner.save(update_fields=["is_staff"])
        for method in ("post", "patch", "delete"):
            response = getattr(client, method)(
                "/api/v1/output-schemas/", {"id": "client_schema"}, format="json"
            )
            assert response.status_code == 405


def test_catalog_pagination_has_a_bounded_page_size(identity):
    _, client, _ = identity
    for index in range(205):
        OutputSchemaRegistry.register(f"catalog_{index}", 1)(ReviewedV2)
    response = client.get("/api/v1/output-schemas/?limit=10000")
    assert response.status_code == 200
    assert response.data["count"] == 206
    assert len(response.data["results"]) == 200
    assert response.data["next"] is not None


def test_unavailable_catalog_returns_safe_configuration_error(identity, monkeypatch):
    _, client, _ = identity

    def unavailable():
        raise OutputSchemaError("Internal schema state must not reach the API response")

    monkeypatch.setattr(OutputSchemaRegistry, "list", staticmethod(unavailable))
    response = client.get("/api/v1/output-schemas/")
    assert response.status_code == 503
    assert "Internal schema state" not in str(response.data)


@pytest.mark.parametrize(
    "reference",
    [
        {"id": "unregistered", "version": 1},
        {"id": "review_decision", "version": 99},
        {"id": "review_decision", "version": True},
        {"id": "review_decision", "version": "1"},
        {"id": "review_decision", "version": 0},
        {"id": "review_decision", "version": 1, "import_path": "module.Model"},
    ],
)
def test_unknown_or_untrusted_schema_references_are_rejected_before_persistence(
    identity, reference
):
    _, client, agent = identity
    response = client.post(
        "/api/v1/workflows/", workflow_body(agent, reference=reference), format="json"
    )
    assert response.status_code == 400, response.data
    assert (
        Workflow.objects.count()
        == WorkflowRun.objects.count()
        == OutboxMessage.objects.count()
        == 0
    )


def test_schema_reference_is_rejected_on_a_non_llm_node(identity):
    _, client, _ = identity
    response = client.post(
        "/api/v1/workflows/",
        {
            "name": "Wrong node kind",
            "nodes": [
                {
                    "id": "approval",
                    "kind": "hitl_approval",
                    "output_schema": {"id": "review_decision", "version": 1},
                }
            ],
            "edges": [],
        },
        format="json",
    )
    assert response.status_code == 400
    assert Workflow.objects.count() == 0


def test_run_freezes_contract_version_and_json_schema_before_outbox_creation(identity):
    owner, client, agent = identity
    workflow = create_workflow(client, agent)
    run = async_to_sync(create_run)(workflow.pk, {"prompt": "Review"}, owner)
    snapshot = deepcopy(run.output_schema_snapshots)
    assert set(snapshot) == {"review"}
    assert snapshot["review"]["id"] == "review_decision"
    assert snapshot["review"]["version"] == 1
    assert set(snapshot["review"]["json_schema"]["properties"]) == {"approved", "score"}
    assert OutboxMessage.objects.get(run=run).topic == "run_workflow"

    OutputSchemaRegistry.register("review_decision", 2)(ReviewedV2)
    new_nodes = deepcopy(workflow.nodes)
    new_nodes[0]["output_schema"]["version"] = 2
    response = client.patch(
        f"/api/v1/workflows/{workflow.pk}/", {"nodes": new_nodes}, format="json"
    )
    assert response.status_code == 200, response.data
    run.refresh_from_db()
    assert run.output_schema_snapshots == snapshot
    assert run.workflow_snapshot["nodes"][0]["output_schema"]["version"] == 1
    second = async_to_sync(create_run)(workflow.pk, {"prompt": "Review again"}, owner)
    assert second.output_schema_snapshots["review"]["version"] == 2


def test_idempotent_command_preserves_frozen_contract_even_if_catalog_version_is_removed(identity):
    owner, client, agent = identity
    workflow = create_workflow(client, agent)
    original = async_to_sync(create_run)(
        workflow.pk, {"prompt": "Review"}, owner, idempotency_key="same"
    )
    snapshot = deepcopy(original.output_schema_snapshots)
    OutputSchemaRegistry._definitions.pop(("review_decision", 1))
    repeated = async_to_sync(create_run)(
        workflow.pk, {"prompt": "Review"}, owner, idempotency_key="same"
    )
    assert repeated.pk == original.pk
    assert repeated.output_schema_snapshots == snapshot
    assert OutboxMessage.objects.count() == 1
    with pytest.raises(ValueError):
        async_to_sync(create_run)(workflow.pk, {"prompt": "New run"}, owner)
    assert WorkflowRun.objects.count() == OutboxMessage.objects.count() == 1


def test_plain_text_runs_have_an_empty_schema_snapshot(identity):
    owner, client, agent = identity
    body = workflow_body(agent)
    del body["nodes"][0]["output_schema"]
    response = client.post("/api/v1/workflows/", body, format="json")
    assert response.status_code == 201, response.data
    run = async_to_sync(create_run)(response.data["id"], {}, owner)
    assert run.output_schema_snapshots == {}


def test_invalid_schema_update_leaves_existing_workflow_definition_unchanged(identity):
    _, client, agent = identity
    workflow = create_workflow(client, agent)
    original = deepcopy(workflow.nodes)
    altered = deepcopy(original)
    altered[0]["output_schema"] = {"id": "removed_contract", "version": 1}
    response = client.patch(f"/api/v1/workflows/{workflow.pk}/", {"nodes": altered}, format="json")
    assert response.status_code == 400
    workflow.refresh_from_db()
    assert workflow.nodes == original
