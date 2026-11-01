import uuid

import pytest

from src.agents.models import Agent
from src.agents.schemas import AgentStatus
from src.agents.services import AgentService
from src.agents.tool_registry import ToolRegistry

pytestmark = pytest.mark.django_db(transaction=True)


async def test_activate_agent_with_registered_tools(agent_factory):
    tool = ToolRegistry.get_specs()[0]
    agent = await agent_factory(status=AgentStatus.DRAFT, tool_ids=[str(tool.id)])
    spec = await AgentService().activate_agent(agent.id)
    assert spec.tool_ids == [tool.id]
    assert (await Agent.objects.aget(pk=agent.pk)).status == "active"


async def test_activate_agent_not_found():
    agent_id = uuid.uuid4()
    with pytest.raises(ValueError, match=f"Agent {agent_id} not found"):
        await AgentService().activate_agent(agent_id)


async def test_llm_only_agent_can_be_activated(agent_factory):
    agent = await agent_factory(status=AgentStatus.DRAFT, tool_ids=[])
    await AgentService().activate_agent(agent.id)
    assert (await Agent.objects.aget(pk=agent.pk)).status == "active"


async def test_unregistered_tool_is_rejected_before_activation(agent_factory):
    agent = await agent_factory(status=AgentStatus.DRAFT, tool_ids=[str(uuid.uuid4())])
    with pytest.raises(ValueError):
        await AgentService().activate_agent(agent.id)
    assert (await Agent.objects.aget(pk=agent.pk)).status == "draft"
