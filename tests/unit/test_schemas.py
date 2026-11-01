from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from src.agents.schemas import AgentSpec, ChatMessage, MessageRole, ToolCall


def test_agent_accepts_stable_registry_tool_ids():
    tool_id = uuid5(NAMESPACE_URL, "platform/tool/example")
    agent = AgentSpec(
        name="Test Agent",
        model_name="gpt-4o",
        system_prompt="Test",
        tool_ids=[str(tool_id)],
    )
    assert agent.tool_ids == [tool_id]


def test_invalid_temperature_is_rejected():
    with pytest.raises(ValidationError):
        AgentSpec(name="Bad", model_name="gpt-4o", system_prompt="Test", temperature=5.0)


def test_extra_agent_fields_are_forbidden():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentSpec(name="Bad", model_name="gpt-4o", system_prompt="Test", unexpected_field="hack")


def test_tool_call_id_is_preserved_in_typed_history():
    call = ToolCall(id="call_provider_123", name="search", arguments={"query": "test"})
    assistant = ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call])
    result = ChatMessage(role=MessageRole.TOOL, content="Found", tool_call_id=call.id)
    assert assistant.tool_calls[0].id == result.tool_call_id
