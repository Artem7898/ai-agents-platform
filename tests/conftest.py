"""Shared fixtures; HTTP/ORM imports stay inside database fixtures."""

import uuid

import httpx
import pytest
import pytest_asyncio
import respx

from src.agents.schemas import AgentSpec


@pytest_asyncio.fixture
async def async_db(transactional_db):
    from asgiref.sync import sync_to_async
    from django.db import connections

    yield
    await sync_to_async(connections.close_all, thread_sensitive=True)()


@pytest_asyncio.fixture
async def agent_factory(async_db):
    from src.agents.models import Agent

    async def create(**kwargs):
        defaults = {
            "name": f"Test Agent {uuid.uuid4().hex[:8]}",
            "model_name": "gpt-4o",
            "status": "active",
            "system_prompt": "You are a helpful assistant.",
            "temperature": 0.7,
            "tool_ids": [],
        }
        defaults.update(kwargs)
        return await Agent.objects.acreate(**defaults)

    return create


@pytest_asyncio.fixture
async def workflow_factory(async_db):
    from src.workflows.models import Workflow

    async def create(**kwargs):
        defaults = {
            "name": f"Test Workflow {uuid.uuid4().hex[:8]}",
            "version": 1,
            "nodes": [
                {
                    "id": "start",
                    "kind": "condition",
                    "config": {"condition": {"path": "enabled", "op": "exists"}},
                }
            ],
            "edges": [],
        }
        defaults.update(kwargs)
        return await Workflow.objects.acreate(**defaults)

    return create


@pytest.fixture
def mock_openai_chat():
    with respx.mock(base_url="https://api.openai.com/v1") as router:
        router.route(path="/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Test response from mocked LLM",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                },
            )
        )
        yield router


@pytest_asyncio.fixture
async def async_client():
    from config.asgi import application

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        yield client
    from asgiref.sync import sync_to_async
    from django.db import connections

    await sync_to_async(connections.close_all, thread_sensitive=True)()


@pytest.fixture
def agent_spec():
    return AgentSpec(
        name="Test Agent",
        model_name="gpt-4o",
        system_prompt="You are a test assistant.",
        temperature=0.7,
    )


@pytest.fixture
def workflow_spec():
    from src.workflows.schemas import WorkflowSpec

    return WorkflowSpec(
        name="Test Workflow",
        nodes=[
            {
                "id": "start",
                "kind": "condition",
                "config": {"condition": {"path": "enabled", "op": "exists"}},
            }
        ],
        edges=[],
    )


@pytest_asyncio.fixture(autouse=True)
async def close_async_connections():
    """Return async ORM connections before pytest-django drops the test DB."""
    from asgiref.sync import sync_to_async
    from django.db import connections

    yield
    await sync_to_async(connections.close_all, thread_sensitive=True)()
