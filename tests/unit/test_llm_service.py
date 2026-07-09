"""
Unit-тесты для LLMService (Этап 6 ТЗ).
Архитектурное решение: Изолируем логику сервиса от инициализации стороннего SDK.
"""
import json
import pytest
import httpx
from unittest.mock import patch # ИМПОРТИРУЕМ ДЛЯ МОКА ГРАНИЦ
from pydantic import BaseModel, Field
import respx
from openai import AsyncOpenAI, APIError

from src.agents.schemas import AgentSpec, ToolSpec
from src.agents.llm_service import LLMService, LLMResponse, _map_tools_to_openai_spec

# ============================================================================
# 1. Тесты на внутренние мапперы
# ============================================================================
class TestMappers:
    def test_map_tools_to_openai_spec_strict_mode(self):
        tool = ToolSpec(
            name="get_weather",
            description="Get weather for a city",
            parameters_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False
            }
        )
        result = _map_tools_to_openai_spec([tool])

        assert len(result) == 1
        assert result[0]["function"]["strict"] is True


# ============================================================================
# 2. Тесты на LLMService.generate_with_tools
# ============================================================================
class TestGenerateWithTools:

    @respx.mock
    @patch("src.agents.llm_service.get_llm_client") # ИЗОЛИРУЕМ СОЗДАНИЕ КЛИЕНТА
    @pytest.mark.asyncio
    async def test_successful_tool_call_parsing(self, mock_get_client):
        """Проверяем парсинг tool_calls."""
        # Подменяем реальный клиент на фейковый (с любым ключом, чтобы пройти __init__)
        mock_get_client.return_value = AsyncOpenAI(api_key="sk-test-fake-key")

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "id": "chatcmpl-123",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "{\"city\": \"Moscow\"}"
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            })
        )

        agent_spec = AgentSpec(name="Test", model_name="gpt-4o", system_prompt="test", tool_ids=[])
        service = LLMService()

        response: LLMResponse = await service.generate_with_tools(
            agent_spec=agent_spec,
            prompt="What's the weather?",
            tools=[ToolSpec(name="get_weather", description="...", parameters_schema={"type": "object", "properties": {}})]
        )

        assert response.content is None
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0]["arguments"] == {"city": "Moscow"}

    @respx.mock
    @patch("src.agents.llm_service.get_llm_client") # ИЗОЛИРУЕМ СОЗДАНИЕ КЛИЕНТА
    @pytest.mark.asyncio
    async def test_llm_api_error_propagates(self, mock_get_client):
        """Проверяем Resilience: проброс ошибок."""
        mock_get_client.return_value = AsyncOpenAI(api_key="sk-test-fake-key")

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": {"message": "Internal Server Error"}})
        )

        agent_spec = AgentSpec(name="Test", model_name="gpt-4o", system_prompt="test")
        service = LLMService()

        with pytest.raises(APIError):
            await service.generate_with_tools(agent_spec=agent_spec, prompt="test")


# ============================================================================
# 3. Тесты на LLMService.generate_structured
# ============================================================================
class TestGenerateStructured:

    class MockOutputModel(BaseModel):
        score: int = Field(..., ge=0, le=100)
        reason: str

    @respx.mock
    @patch("src.agents.llm_service.get_llm_client") # ИЗОЛИРУЕМ СОЗДАНИЕ КЛИЕНТА
    @pytest.mark.asyncio
    async def test_strict_structured_output_success(self, mock_get_client):
        """Проверяем преобразование JSON в Pydantic модель."""
        mock_get_client.return_value = AsyncOpenAI(api_key="sk-test-fake-key")

        mock_response = {"score": 95, "reason": "Excellent performance"}

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(mock_response)
                    }
                }]
            })
        )

        agent_spec = AgentSpec(name="Test", model_name="gpt-4o", system_prompt="test")
        service = LLMService()

        result = await service.generate_structured(
            agent_spec=agent_spec,
            prompt="Analyze this",
            target_schema=self.MockOutputModel
        )

        assert isinstance(result, self.MockOutputModel)
        assert result.score == 95

    @respx.mock
    @patch("src.agents.llm_service.get_llm_client") # ИЗОЛИРУЕМ СОЗДАНИЕ КЛИЕНТА
    @pytest.mark.asyncio
    async def test_structured_output_validation_fails_on_invalid_data(self, mock_get_client):
        """Проверяем, что невалидные данные ломают Pydantic (защита БД)."""
        mock_get_client.return_value = AsyncOpenAI(api_key="sk-test-fake-key")

        invalid_response = {"score": "not_a_number", "reason": "Bad data"}

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(invalid_response)
                    }
                }]
            })
        )

        agent_spec = AgentSpec(name="Test", model_name="gpt-4o", system_prompt="test")
        service = LLMService()

        with pytest.raises(Exception):
            await service.generate_structured(
                agent_spec=agent_spec,
                prompt="test",
                target_schema=self.MockOutputModel
            )