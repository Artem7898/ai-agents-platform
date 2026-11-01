"""Agent activation validates configuration, including optional tool grants."""

import uuid

from django.core.exceptions import ObjectDoesNotExist
from nova.core.tracing import nova_span

from .models import Agent
from .schemas import AgentSpec, AgentStatus
from .tool_registry import ToolRegistry


class AgentService:
    async def activate_agent(self, agent_id: uuid.UUID) -> AgentSpec:
        with nova_span("agent.service.activate", agent_id=str(agent_id)):
            try:
                agent = await Agent.objects.aget(id=agent_id)
            except ObjectDoesNotExist as exc:
                raise ValueError(f"Agent {agent_id} not found") from exc
            spec = agent.to_spec()
            if not spec.system_prompt.strip() or not spec.model_name.strip():
                raise ValueError("Agent requires a system prompt and model name")
            for tool_id in spec.tool_ids:
                ToolRegistry.get_spec_by_id(tool_id)
            agent.status = AgentStatus.ACTIVE.value
            await agent.asave(update_fields=["status", "updated_at"])
            return spec
