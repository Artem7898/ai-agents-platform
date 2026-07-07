import pytest
import uuid
from pydantic import ValidationError
from src.agents.schemas import AgentSpec

class TestAgentSpec:
    def test_valid_agent_creation(self, agent_spec: AgentSpec):
        """Проверяем, что валидная спека создается без ошибок."""
        assert agent_spec.name == "Test Agent"
        assert agent_spec.model_name == "gpt-4o"
        assert isinstance(agent_spec.tool_ids, list)

    def test_invalid_temperature_raises(self):
        """NFR: temperature должна быть в пределах [0.0, 2.0]."""
        with pytest.raises(ValidationError):
            AgentSpec(
                name="Bad Agent",
                model_name="gpt-4o",
                system_prompt="test",
                temperature=5.0 # Выходит за границы
            )

    def test_extra_fields_forbidden(self):
        """Строгая валидация: лишние поля запрещены."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="Bad Agent",
                model_name="gpt-4o",
                system_prompt="test",
                unexpected_field="hack" # Запрещено схемой!
            )
        assert "extra_forbidden" in str(exc_info.value)