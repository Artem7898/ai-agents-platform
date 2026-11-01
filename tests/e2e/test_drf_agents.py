"""Definition CRUD requires a token and always assigns the current owner."""

import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def identity():
    user = await sync_to_async(get_user_model().objects.create_user)(
        username=f"agent-api-{uuid.uuid4().hex}", password="test-only"
    )
    token = await Token.objects.acreate(user=user)
    return user, {"Authorization": f"Token {token.key}"}


async def test_create_agent_via_drf(async_client):
    owner, headers = await identity()
    response = await async_client.post(
        "/api/v1/agents/",
        headers=headers,
        json={
            "name": "E2E Test Agent",
            "model_name": "gpt-4o",
            "system_prompt": "You are an E2E test assistant.",
            "temperature": 0.5,
            "tool_ids": [],
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == "E2E Test Agent"
    assert data["status"] == "draft"
    assert data["owner"] == owner.pk


async def test_get_agent_via_drf(async_client, agent_factory):
    owner, headers = await identity()
    agent = await agent_factory(name="Factory Agent", owner=owner)
    response = await async_client.get(f"/api/v1/agents/{agent.id}/", headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "Factory Agent"
