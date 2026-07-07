import uuid
from django.core.exceptions import ObjectDoesNotExist
from nova.core.tracing import nova_span  # ИСПРАВЛЕННЫЙ ИМПОРТ
from .models import Agent
from .schemas import AgentSpec, AgentStatus


class AgentService:
    """Бизнес-логика домена Agents."""

    async def activate_agent(self, agent_id: uuid.UUID) -> AgentSpec:
        with nova_span("agent.service.activate", agent_id=str(agent_id)):
            try:
                agent = await Agent.objects.aget(id=agent_id)
            except ObjectDoesNotExist:
                raise ValueError(f"Agent {agent_id} not found")

            if not agent.tool_ids:
                raise ValueError("Agent must have at least one tool to activate")

            agent.status = AgentStatus.ACTIVE.value
            await agent.asave()

            return agent.to_spec()