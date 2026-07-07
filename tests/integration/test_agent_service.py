import pytest
import pytest_asyncio
import uuid

from src.agents.services import AgentService
from src.agents.schemas import AgentStatus


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_activate_agent_success(agent_factory):
    """Use-case: успешная активация агента, у которого ЕСТЬ валидные инструменты."""
    # Arrange: Создаем агента С инструментами (передаем валидные JSON строки, а не UUID объекты)
    real_tool_uuid = uuid.uuid4()
    agent = await agent_factory(
        status=AgentStatus.DRAFT,
        system_prompt="Valid prompt",
        tool_ids=[str(real_tool_uuid)],  # <--- ИСПРАВЛЕНО: Строки корректно сохраняются в JSONField
    )
    service = AgentService()

    # Act
    activated_spec = await service.activate_agent(agent.id)

    # Assert
    assert activated_spec.status == AgentStatus.ACTIVE

    # Проверяем, что изменения сохранились в БД
    from src.agents.models import Agent
    updated_agent = await Agent.objects.aget(id=agent.id)
    assert updated_agent.status == AgentStatus.ACTIVE.value


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_activate_agent_not_found():
    """Use-case: активация несуществующего агента."""
    service = AgentService()
    fake_id = uuid.uuid4()

    with pytest.raises(ValueError, match=f"Agent {fake_id} not found"):
        await service.activate_agent(fake_id)


@pytest.mark.asyncio
@pytest.mark.django.db(transaction=True)
async def test_activate_agent_without_tools_fails(agent_factory):
    """Use-case: нельзя активировать агента без инструментов."""
    agent = await agent_factory(
        system_prompt="Valid prompt",
        tool_ids=[],  # Пустой список - валидный JSON, тест "без инструментов" работает корректно
    )
    service = AgentService()

    with pytest.raises(ValueError, match="must have at least one tool"):
        await service.activate_agent(agent.id)