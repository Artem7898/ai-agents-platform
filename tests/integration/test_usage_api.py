"""Accounting APIs protect owner history and require explicit operator actions."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, local

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from src.runs.models import WorkflowRun
from src.runtimes.models import LLMRuntime
from src.usage.models import LLMInvocation, LLMRate, OwnerQuota, QuotaWindow
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def identities():
    users = {}
    for name in ("alice", "bob", "staff"):
        user = get_user_model().objects.create_user(
            username=f"{name}-{uuid.uuid4().hex}", is_staff=name == "staff"
        )
        token = Token.objects.create(user=user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        users[name] = user, client
    return users


def make_run(owner, **kwargs):
    workflow = Workflow.objects.create(
        owner=owner,
        name=f"usage-{uuid.uuid4().hex}",
        nodes=[
            {
                "id": "condition",
                "kind": "condition",
                "config": {"condition": {"path": "enabled", "op": "exists"}},
            }
        ],
        edges=[],
    )
    return WorkflowRun.objects.create(workflow=workflow, billing_owner=owner, **kwargs)


def make_invocation(owner, **kwargs):
    defaults = {
        "owner": owner,
        "run": make_run(owner),
        "node_id": "agent",
        "logical_call": 1,
        "attempt": 1,
        "model_name": "model",
        "provider": "openai",
        "operation": "complete",
        "status": "unknown",
        "reserved_tokens": 100,
        "max_output_tokens": 50,
    }
    defaults.update(kwargs)
    return LLMInvocation.objects.create(**defaults)


def make_window(owner, **kwargs):
    quota = OwnerQuota.objects.create(owner=owner, token_limit=1000, request_limit=10)
    return QuotaWindow.objects.create(
        quota=quota,
        starts_at=quota.created_at,
        ends_at=quota.created_at + timedelta(seconds=quota.period_seconds),
        **kwargs,
    )


def rate_body(**kwargs):
    return {
        "runtime": None,
        "model_name": "model",
        "version": "2026-11",
        "currency": "USD",
        "input_per_million": "1.25",
        "output_per_million": "5.00",
        **kwargs,
    }


def test_accounting_endpoints_require_token(identities):
    client = APIClient()
    for endpoint in ("llm-calls", "llm-quotas", "llm-quota-windows", "llm-rates"):
        assert client.get(f"/api/v1/{endpoint}/").status_code == 401


def test_invocation_visibility_uses_frozen_owner_and_omits_private_data(identities):
    owner, alice = identities["alice"]
    other_owner, bob = identities["bob"]
    _, staff = identities["staff"]
    invocation = make_invocation(owner, execution_token=uuid.uuid4())
    invocation.run.workflow.owner = other_owner
    invocation.run.workflow.save()
    make_invocation(other_owner)

    url = f"/api/v1/llm-calls/{invocation.pk}/"
    response = alice.get(url)
    assert response.status_code == 200, response.data
    body = response.json()
    assert body["owner"] == owner.pk
    assert body["run"] == str(invocation.run_id)
    assert body["prompt_tokens"] is None
    assert body["total_tokens"] is None
    assert body["cost"] is None
    assert {
        "execution_token",
        "input_data",
        "output_data",
        "prompt",
        "messages",
        "secret_ref",
        "api_key",
    }.isdisjoint(body)
    assert bob.get(url).status_code == 404
    assert len(alice.get("/api/v1/llm-calls/").json()["results"]) == 1
    assert len(staff.get("/api/v1/llm-calls/").json()["results"]) == 2
    assert alice.patch(url, {"total_tokens": 0}, format="json").status_code == 405
    assert staff.delete(url).status_code == 405


def test_invocation_list_filters_run_and_caps_page_size(identities):
    owner, alice = identities["alice"]
    run = make_run(owner)
    LLMInvocation.objects.bulk_create(
        [
            LLMInvocation(
                owner=owner,
                run=run,
                node_id="node",
                logical_call=index,
                attempt=1,
                model_name="model",
                provider="openai",
                operation="complete",
                reserved_tokens=10,
                max_output_tokens=5,
            )
            for index in range(205)
        ]
    )
    other = make_invocation(owner)
    response = alice.get(f"/api/v1/llm-calls/?run_id={run.pk}&limit=1000")
    assert response.status_code == 200, response.data
    body = response.json()
    assert body["count"] == 205
    assert len(body["results"]) == 200
    assert body["next"] is not None
    assert {item["run"] for item in body["results"]} == {str(run.pk)}
    assert alice.get(f"/api/v1/llm-calls/?run_id={other.run_id}").json()["count"] == 1
    assert alice.get("/api/v1/llm-calls/?run_id=invalid").status_code == 400


def test_only_staff_can_manage_quota_and_anchor_is_immutable(identities):
    owner, alice = identities["alice"]
    other_owner, bob = identities["bob"]
    _, staff = identities["staff"]
    body = {"owner": owner.pk, "token_limit": 1000, "request_limit": 10}
    assert alice.post("/api/v1/llm-quotas/", body, format="json").status_code == 403
    response = staff.post("/api/v1/llm-quotas/", body, format="json")
    assert response.status_code == 201, response.data
    assert response.json()["period_seconds"] == 86400
    quota_id = response.json()["id"]
    url = f"/api/v1/llm-quotas/{quota_id}/"
    assert alice.get(url).status_code == 200
    assert bob.get(url).status_code == 404
    assert bob.get("/api/v1/llm-quotas/").json()["results"] == []
    assert alice.patch(url, {"token_limit": None}, format="json").status_code == 403
    for payload in (
        {"period_seconds": 60},
        {"owner": other_owner.pk},
        {"token_limit": -1},
        {"token_limit": 2**63},
        {"request_limit": 2**63},
    ):
        assert staff.patch(url, payload, format="json").status_code == 400
    response = staff.patch(url, {"token_limit": None, "enabled": False}, format="json")
    assert response.status_code == 200, response.data
    assert response.json()["token_limit"] is None
    assert staff.delete(url).status_code == 405
    assert staff.post(url + "reset/", {}, format="json").status_code == 404
    quota = OwnerQuota.objects.get(pk=quota_id)
    assert quota.owner_id == owner.pk
    assert quota.period_seconds == 86400
    assert not quota.enabled


def test_quota_windows_are_owner_scoped_and_read_only(identities):
    owner, alice = identities["alice"]
    other_owner, bob = identities["bob"]
    _, staff = identities["staff"]
    window = make_window(owner, used_tokens=20, reserved_tokens=100, used_calls=1)
    make_window(other_owner)
    url = f"/api/v1/llm-quota-windows/{window.pk}/"
    response = alice.get(url)
    assert response.status_code == 200, response.data
    assert response.json()["used_tokens"] == 20
    assert response.json()["reserved_tokens"] == 100
    assert bob.get(url).status_code == 404
    assert len(alice.get("/api/v1/llm-quota-windows/").json()["results"]) == 1
    assert len(staff.get("/api/v1/llm-quota-windows/").json()["results"]) == 2
    assert staff.patch(url, {"used_tokens": 0}, format="json").status_code == 405
    assert staff.delete(url).status_code == 405


def test_rates_are_immutable_versions_and_visibility_follows_runtime_owner(identities):
    owner, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    runtime = LLMRuntime.objects.create(
        owner=owner, name="Owned", allowed_models=["model"], secret_ref="test-only"
    )
    assert alice.post("/api/v1/llm-rates/", rate_body(), format="json").status_code == 403
    global_response = staff.post("/api/v1/llm-rates/", rate_body(), format="json")
    assert global_response.status_code == 201, global_response.data
    response = staff.post("/api/v1/llm-rates/", rate_body(runtime=str(runtime.pk)), format="json")
    assert response.status_code == 201, response.data
    rate_id = response.json()["id"]
    url = f"/api/v1/llm-rates/{rate_id}/"
    assert alice.get(url).status_code == 200
    assert bob.get(url).status_code == 404
    assert len(alice.get("/api/v1/llm-rates/").json()["results"]) == 2
    assert len(bob.get("/api/v1/llm-rates/").json()["results"]) == 1
    assert staff.patch(url, {"input_per_million": "0"}, format="json").status_code == 405
    assert staff.delete(url).status_code == 405
    assert (
        staff.post(
            "/api/v1/llm-rates/", rate_body(runtime=str(runtime.pk)), format="json"
        ).status_code
        == 400
    )
    assert LLMRate.objects.count() == 2


def test_reconcile_requires_staff_and_rejects_active_lease(identities):
    owner, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    token = uuid.uuid4()
    invocation = make_invocation(owner, execution_token=token, dispatched_at=timezone.now())
    WorkflowRun.objects.filter(pk=invocation.run_id).update(
        status="running",
        execution_token=token,
        lease_expires_at=timezone.now() + timedelta(minutes=5),
    )
    url = f"/api/v1/llm-calls/{invocation.pk}/reconcile/"
    body = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    assert alice.post(url, body, format="json").status_code == 403
    assert bob.post(url, body, format="json").status_code == 403
    response = staff.post(url, body, format="json")
    assert response.status_code == 409, response.data
    assert str(token) not in response.content.decode()
    invocation.refresh_from_db()
    assert invocation.total_tokens is None
    assert invocation.reservation_active


def test_reconcile_settles_unknown_once_and_rejects_conflicts(identities):
    owner, _ = identities["alice"]
    _, staff = identities["staff"]
    window = make_window(owner, reserved_tokens=100, used_calls=1)
    invocation = make_invocation(owner, quota_window=window, dispatched_at=timezone.now())
    url = f"/api/v1/llm-calls/{invocation.pk}/reconcile/"
    for body in (
        {},
        {"total_tokens": -1},
        {"total_tokens": 2**63},
        {"total_tokens": 2**63 - 1, "prompt_tokens": 2**63},
        {"prompt_tokens": 10},
        {"total_tokens": 5, "prompt_tokens": 10},
        {"not_sent": True, "total_tokens": 0},
    ):
        assert staff.post(url, body, format="json").status_code == 400
    body = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    response = staff.post(url, body, format="json")
    assert response.status_code == 200, response.data
    assert response.json()["usage_status"] == "reconciled"
    assert response.json()["reservation_active"] is False
    assert response.json()["total_tokens"] == 25
    assert staff.post(url, body, format="json").status_code == 200
    assert staff.post(url, {"total_tokens": 30}, format="json").status_code == 409
    window.refresh_from_db()
    assert window.used_tokens == 25
    assert window.reserved_tokens == 0


def test_reconcile_not_sent_releases_hold_and_reported_usage_is_protected(identities):
    owner, _ = identities["alice"]
    _, staff = identities["staff"]
    window = make_window(owner, reserved_tokens=100, reserved_calls=1)
    invocation = make_invocation(owner, quota_window=window)
    url = f"/api/v1/llm-calls/{invocation.pk}/reconcile/"
    response = staff.post(url, {"not_sent": True}, format="json")
    assert response.status_code == 200, response.data
    assert staff.post(url, {"not_sent": True}, format="json").status_code == 200
    window.refresh_from_db()
    assert window.reserved_tokens == 0
    assert window.reserved_calls == 0

    reported = make_invocation(
        owner,
        status="succeeded",
        usage_status="reported",
        prompt_tokens=2,
        completion_tokens=1,
        total_tokens=3,
        reservation_active=False,
    )
    response = staff.post(
        f"/api/v1/llm-calls/{reported.pk}/reconcile/", {"total_tokens": 4}, format="json"
    )
    assert response.status_code == 409
    reported.refresh_from_db()
    assert reported.total_tokens == 3


@pytest.mark.postgresql
@pytest.mark.parametrize("resource,model", [("llm-quotas", OwnerQuota), ("llm-rates", LLMRate)])
def test_concurrent_duplicate_create_returns_safe_conflict(
    identities, monkeypatch, resource, model
):
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent unique-constraint admission requires PostgreSQL")
    owner, _ = identities["alice"]
    staff, _ = identities["staff"]
    token = Token.objects.get(user=staff)
    body = {"owner": owner.pk, "token_limit": 1000} if model is OwnerQuota else rate_body()
    validation_barrier = Barrier(2)
    thread_state = local()
    original_clean = model.full_clean

    def clean_before_competing_insert(instance, *args, **kwargs):
        result = original_clean(instance, *args, **kwargs)
        thread_state.clean_count = getattr(thread_state, "clean_count", 0) + 1
        if thread_state.clean_count == 2:
            # Both serializer and model validations have passed in both requests.
            # Their remaining duplicate insert is decided by the SQL constraint.
            validation_barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(model, "full_clean", clean_before_competing_insert)

    def create():
        close_old_connections()
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
            return client.post(f"/api/v1/{resource}/", body, format="json")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: create(), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == {
        "detail": "The accounting configuration conflicts with an existing record."
    }
    assert model.objects.count() == 1
