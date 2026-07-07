import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import respx
from httpx import AsyncClient, ASGITransport

from config.asgi import application
from src.agents.models import Agent
from src.agents.schemas import AgentSpec, AgentStatus
from src.workflows.models import Workflow
from src.workflows.schemas import WorkflowSpec, WorkflowNodeSpec, WorkflowEdgeSpec, NodeKind
from src.runs.models import WorkflowRun


# ==============================================================================
# 1. DATABASE FIXTURES
# ==============================================================================
@pytest_asyncio.fixture
async def async_db(db):
    """
    Связывает pytest-asyncio с транзакционной БД pytest-django.
    Позволяет использовать асинхронный ORM в тестах без потери данных.
    """
    yield


# ==============================================================================
# 2. FACTORY FIXTURES (Адаптированы под текущие модели)
# ==============================================================================
@pytest_asyncio.fixture
async def agent_factory(async_db) -> callable:
    """Factory для создания тестовых агентов в БД."""

    async def _create_agent(**kwargs) -> Agent:
        defaults = {
            "id": uuid.uuid4(),
            "name": f"Test Agent {uuid.uuid4().hex[:8]}",
            "model_name": "gpt-4o",
            "status": AgentStatus.DRAFT,
            "system_prompt": "You are a helpful assistant.",
            "temperature": 0.7,
            "tool_ids": [],
        }
        defaults.update(kwargs)
        agent = Agent(**defaults)
        await agent.asave()
        return agent

    return _create_agent


@pytest_asyncio.fixture
async def workflow_factory(async_db) -> callable:
    """Factory для создания тестовых workflow в БД."""

    async def _create_workflow(**kwargs) -> Workflow:
        defaults = {
            "id": uuid.uuid4(),
            "name": f"Test Workflow {uuid.uuid4().hex[:8]}",
            "version": 1,
            "nodes": [],
            "edges": [],
        }
        defaults.update(kwargs)
        workflow = Workflow(**defaults)
        await workflow.asave()
        return workflow

    return _create_workflow


# ==============================================================================
# 3. LLM MOCK FIXTURES (respx)
# ==============================================================================
@pytest.fixture
def mock_openai_chat() -> respx.MockRouter:
    """Мокает OpenAI Chat Completions API (возвращает обычный JSON)."""
    with respx.mock(base_url="https://api.openai.com/v1") as router:
        router.route(path="/chat/completions").mock(
            return_value=respx.Response(200, json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4o",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Test response from mocked LLM"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
            })
        )
        yield router


# ==============================================================================
# 4. HTTP CLIENT FIXTURES (Для E2E тестов)
# ==============================================================================
@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """
    Async HTTP клиент для тестирования API.
    Использует ASGITransport для прямого вызова ASGI без реального порта.
    """
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ==============================================================================
# 5. DOMAIN FIXTURES (Чистые Pydantic-объекты без БД)
# ==============================================================================
@pytest.fixture
def agent_spec() -> AgentSpec:
    """Тестовая Pydantic-спека агента (соответствует текущей схеме)."""
    return AgentSpec(
        name="Test Agent",
        model_name="gpt-4o",      # ИСПРАВЛЕНО
        system_prompt="You are a test assistant.",
        temperature=0.7,           # ИСПРАВЛЕНО
        tool_ids=[uuid.uuid4()],
    )


@pytest.fixture
def workflow_spec() -> WorkflowSpec:
    """Тестовая Pydantic-спека workflow с простым DAG."""
    node1 = WorkflowNodeSpec(id="start", kind=NodeKind.LLM_CALL, agent_id=uuid.uuid4())
    node2 = WorkflowNodeSpec(id="end", kind=NodeKind.TOOL_EXEC, tool_id=uuid.uuid4())
    edge = WorkflowEdgeSpec(source="start", target="end")

    return WorkflowSpec(
        name="Test Workflow",
        version=1,
        nodes=[node1, node2],
        edges=[edge],
    )