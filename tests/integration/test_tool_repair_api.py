"""Repair policy validation and immutable run configuration at the API boundary."""

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from src.runs.models import WorkflowRun
from src.runs.services import create_run
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def identity():
    user = get_user_model().objects.create_user(username="repair-policy-owner")
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return user, client


def definition(**kwargs):
    return {
        "name": "Repair policy",
        "nodes": [{"id": "approval", "kind": "hitl_approval"}],
        "edges": [],
        **kwargs,
    }


@pytest.mark.parametrize("limit", [0, 2, 5])
def test_repair_limit_round_trips_and_is_frozen_for_existing_run(identity, limit):
    user, client = identity
    response = client.post(
        "/api/v1/workflows/", definition(limits={"max_tool_repairs": limit}), format="json"
    )
    assert response.status_code == 201, response.data
    assert response.data["limits"]["max_tool_repairs"] == limit
    workflow = Workflow.objects.get(pk=response.data["id"])
    run = async_to_sync(create_run)(workflow.pk, {}, user)
    replacement = (limit + 1) % 6
    response = client.patch(
        f"/api/v1/workflows/{workflow.pk}/",
        {"limits": {"max_tool_repairs": replacement}},
        format="json",
    )
    assert response.status_code == 200, response.data
    run.refresh_from_db()
    assert run.workflow_snapshot["limits"]["max_tool_repairs"] == limit
    assert Workflow.objects.get(pk=workflow.pk).limits["max_tool_repairs"] == replacement


@pytest.mark.parametrize("limit", [-1, 6, True, "2", 1.5])
def test_invalid_repair_budget_is_rejected_before_workflow_persistence(identity, limit):
    _, client = identity
    response = client.post(
        "/api/v1/workflows/", definition(limits={"max_tool_repairs": limit}), format="json"
    )
    assert response.status_code == 400, response.data
    assert Workflow.objects.count() == WorkflowRun.objects.count() == 0


def test_new_runs_get_default_repair_policy_when_workflow_limits_are_omitted(identity):
    user, client = identity
    response = client.post("/api/v1/workflows/", definition(), format="json")
    assert response.status_code == 201, response.data
    run = async_to_sync(create_run)(response.data["id"], {}, user)
    assert run.workflow_snapshot["limits"]["max_tool_repairs"] == 2
