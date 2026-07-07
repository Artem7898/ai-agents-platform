import pytest
import uuid
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_create_agent_via_drf(async_client: AsyncClient):
    """E2E: создание агента через DRF API (плоская схема)."""
    payload = {
        "name": "E2E Test Agent",
        "model_name": "gpt-4o",  # Плоское поле (было llm_config.model_name)
        "system_prompt": "You are an E2E test assistant.",
        "temperature": 0.5,  # Плоское поле (было llm_config.temperature)
        "tool_ids": [],  # JSONField массив
    }

    response = await async_client.post("/api/v1/agents/", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "E2E Test Agent"
    assert data["status"] == "draft"  # Дефолтный статус из модели


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_agent_via_drf(async_client: AsyncClient, agent_factory):
    """E2E: получение агента после создания."""
    # Используем фабрику из conftest.py
    agent = await agent_factory(name="Factory Agent")

    response = await async_client.get(f"/api/v1/agents/{agent.id}/")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Factory Agent"
    assert "model_name" in data