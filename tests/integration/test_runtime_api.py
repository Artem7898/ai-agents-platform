"""Exercise token-authenticated runtime CRUD, visibility and operator boundaries."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from src.agents.models import Agent
from src.runtimes.models import LLMRuntime

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def identities():
    users = {}
    for name in ("alice", "bob", "staff"):
        user = get_user_model().objects.create_user(username=name, is_staff=name == "staff")
        token = Token.objects.create(user=user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        users[name] = user, client
    return users


def runtime_body(**kwargs):
    return {"name": "Cloud", "allowed_models": ["gpt-4o"], "secret_ref": "cloud-key", **kwargs}


def test_runtime_api_requires_token_and_only_staff_can_write(identities):
    _, alice = identities["alice"]
    user, _ = identities["staff"]
    runtime = LLMRuntime.objects.create(
        owner=user, name="Protected", allowed_models=["model"], secret_ref="test-key"
    )
    assert APIClient().get("/api/v1/runtimes/").status_code == 401
    assert alice.post("/api/v1/runtimes/", runtime_body(), format="json").status_code == 403
    for method in ("put", "patch", "delete"):
        response = getattr(alice, method)(
            f"/api/v1/runtimes/{runtime.pk}/", {"name": "Denied"}, format="json"
        )
        assert response.status_code == 403
    assert LLMRuntime.objects.count() == 1


def test_staff_assigns_runtime_owner_and_secret_alias_is_write_only(identities):
    owner, alice = identities["alice"]
    _, bob = identities["bob"]
    _, staff = identities["staff"]
    response = staff.post(
        "/api/v1/runtimes/", runtime_body(owner=owner.pk, revision=200), format="json"
    )
    assert response.status_code == 201, response.data
    data = response.json()
    assert data["owner"] == owner.pk
    assert data["revision"] == 1
    assert "secret_ref" not in data
    runtime = LLMRuntime.objects.get(pk=data["id"])
    assert runtime.secret_ref == "cloud-key"
    url = f"/api/v1/runtimes/{runtime.pk}/"
    assert alice.get(url).status_code == 200
    assert "secret_ref" not in alice.get(url).json()
    assert bob.get(url).status_code == 404
    assert bob.get("/api/v1/runtimes/").json()["results"] == []
    assert len(staff.get("/api/v1/runtimes/").json()["results"]) == 1


def test_staff_runtime_owner_defaults_to_requester_but_can_be_unassigned(identities):
    owner, staff = identities["staff"]
    response = staff.post("/api/v1/runtimes/", runtime_body(), format="json")
    assert response.status_code == 201, response.data
    assert response.json()["owner"] == owner.pk
    url = f"/api/v1/runtimes/{response.json()['id']}/"
    response = staff.patch(url, {"owner": None}, format="json")
    assert response.status_code == 200, response.data
    assert response.json()["owner"] is None
    assert response.json()["revision"] == 2


def test_staff_patch_validates_before_save_and_updates_revision(identities):
    _, staff = identities["staff"]
    response = staff.post("/api/v1/runtimes/", runtime_body(), format="json")
    runtime_id = response.json()["id"]
    url = f"/api/v1/runtimes/{runtime_id}/"
    response = staff.patch(url, {"allowed_models": []}, format="json")
    assert response.status_code == 400
    response = staff.patch(url, {"base_url": "https://attacker.example/v1"}, format="json")
    assert response.status_code == 400
    response = staff.patch(url, {"capabilities": {"tools": "false"}}, format="json")
    assert response.status_code == 400
    runtime = LLMRuntime.objects.get(pk=runtime_id)
    assert runtime.revision == 1
    response = staff.patch(
        url, {"enabled": False, "secret_ref": "rotated-cloud-key"}, format="json"
    )
    assert response.status_code == 200, response.data
    assert response.json()["enabled"] is False
    assert response.json()["revision"] == 2
    assert "secret_ref" not in response.json()
    runtime.refresh_from_db()
    assert runtime.secret_ref == "rotated-cloud-key"


def test_runtime_assigned_to_agent_cannot_be_deleted(identities):
    owner, staff = identities["staff"]
    runtime = LLMRuntime.objects.create(
        owner=owner, name="In use", allowed_models=["gpt-4o"], secret_ref="test-key"
    )
    Agent.objects.create(
        owner=owner, name="Bound", model_name="gpt-4o", system_prompt="Test", runtime=runtime
    )
    response = staff.delete(f"/api/v1/runtimes/{runtime.pk}/")
    assert response.status_code == 409
    assert LLMRuntime.objects.filter(pk=runtime.pk).exists()


def test_staff_can_delete_unassigned_runtime(identities):
    _, staff = identities["staff"]
    runtime = LLMRuntime.objects.create(
        name="Unused", allowed_models=["model"], secret_ref="test-key"
    )
    response = staff.delete(f"/api/v1/runtimes/{runtime.pk}/")
    assert response.status_code == 204
    assert not LLMRuntime.objects.filter(pk=runtime.pk).exists()
