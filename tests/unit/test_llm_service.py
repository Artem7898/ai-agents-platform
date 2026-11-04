"""Provider boundary tests: no credentials, live HTTP or database are required."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, Field

import src.agents.llm_service as llm_module
from src.agents.llm_service import (
    LLMResponseError,
    LLMService,
    StructuredOutputValidationError,
    _map_tools_to_openai_spec,
)
from src.agents.schemas import AgentSpec, ChatMessage, MessageRole, ToolCall, ToolSpec


def completion(*, content="Answer", calls=None, refusal=None, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=calls, refusal=refusal),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


@pytest.fixture
def agent():
    return AgentSpec(name="Test", model_name="gpt-4o", system_prompt="Be helpful.")


@pytest.fixture
def provider(monkeypatch):
    create = AsyncMock(return_value=completion())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_module, "get_llm_client", lambda: client)
    return create


async def test_tool_response_preserves_call_id_arguments_and_usage(agent, provider):
    provider.return_value = completion(
        content=None,
        calls=[
            SimpleNamespace(
                id="call_weather_1",
                type="function",
                function=SimpleNamespace(name="weather", arguments='{"city":"Moscow"}'),
            )
        ],
        finish_reason="tool_calls",
    )
    result = await LLMService().generate_with_tools(agent, "Weather?")

    assert result.tool_calls == [
        ToolCall(id="call_weather_1", name="weather", arguments={"city": "Moscow"})
    ]
    assert result.content is None
    assert result.usage.total_tokens == 18
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.as_message().role == MessageRole.ASSISTANT
    assert result.as_message().tool_calls[0].id == "call_weather_1"


async def test_followup_maps_typed_history_without_empty_user_message(agent, provider):
    history = [
        ChatMessage(role=MessageRole.USER, content="Weather?"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[ToolCall(id="call_1", name="weather", arguments={"city": "Moscow"})],
        ),
        ChatMessage(role=MessageRole.TOOL, content="Snow", tool_call_id="call_1"),
    ]
    await LLMService().generate_with_tools(agent, context_history=history, max_output_tokens=250)

    sent = provider.await_args.kwargs
    messages = sent["messages"]
    assert messages[0] == {"role": "system", "content": "Be helpful."}
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    call = messages[2]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert json.loads(call["function"]["arguments"]) == {"city": "Moscow"}
    assert messages[3]["tool_call_id"] == "call_1"
    assert 250 in (sent.get("max_tokens"), sent.get("max_completion_tokens"))


@pytest.mark.parametrize(
    "raw_arguments", ["not-json", "[]", '"string"', '{"value":NaN}', '{"value":Infinity}']
)
async def test_non_object_tool_arguments_fail_before_dispatch(agent, provider, raw_arguments):
    provider.return_value = completion(
        content=None,
        calls=[
            SimpleNamespace(
                id="call_invalid",
                type="function",
                function=SimpleNamespace(name="weather", arguments=raw_arguments),
            )
        ],
        finish_reason="tool_calls",
    )
    with pytest.raises(LLMResponseError):
        await LLMService().generate_with_tools(agent, "Weather?")


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_incomplete_response_is_not_accepted_as_final_answer(agent, provider, finish_reason):
    provider.return_value = completion(finish_reason=finish_reason)
    with pytest.raises(LLMResponseError):
        await LLMService().generate_with_tools(agent, "Question")


async def test_refusal_is_reported_as_a_provider_result_error(agent, provider):
    provider.return_value = completion(content=None, refusal="Cannot help with that request")
    with pytest.raises(LLMResponseError):
        await LLMService().generate_with_tools(agent, "Question")


@pytest.mark.parametrize("limit", [0, 32769])
async def test_output_limit_is_bounded_before_provider_call(agent, provider, limit):
    with pytest.raises(ValueError):
        await LLMService().generate_with_tools(agent, "Question", max_output_tokens=limit)
    provider.assert_not_awaited()


class Score(BaseModel):
    score: int = Field(ge=0, le=100)
    reason: str = ""


async def test_structured_output_uses_strict_schema_and_validates_return_value(agent, provider):
    provider.return_value = completion(content='{"score":95,"reason":"Passed"}')
    result = await LLMService().generate_structured(agent, "Score it", Score)
    assert result == Score(score=95, reason="Passed")

    contract = provider.await_args.kwargs["response_format"]["json_schema"]
    assert contract["strict"] is True
    assert contract["schema"]["additionalProperties"] is False
    assert set(contract["schema"]["required"]) == {"score", "reason"}


async def test_structured_output_rejects_invalid_model_data(agent, provider):
    provider.return_value = completion(content='{"score":150,"reason":"Outside range"}')
    with pytest.raises(StructuredOutputValidationError):
        await LLMService().generate_structured(agent, "Score it", Score)


async def test_tracing_span_remains_open_during_provider_await(agent, provider, monkeypatch):
    events = []

    @contextmanager
    def span(*_args, **_kwargs):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    async def create(**_kwargs):
        events.append("api-await")
        return completion()

    monkeypatch.setattr(llm_module, "safe_nova_span", span)
    provider.side_effect = create
    coroutine = LLMService().generate_with_tools(agent, "Question")
    assert events == []
    await coroutine
    assert events == ["enter", "api-await", "exit"]


def test_tool_schema_is_strict_without_mutating_domain_definition():
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}}
    tool = ToolSpec(name="weather", description="Get weather", parameters_schema=parameters)
    exported = _map_tools_to_openai_spec([tool])[0]["function"]

    assert exported["strict"] is True
    assert exported["parameters"]["additionalProperties"] is False
    assert exported["parameters"]["required"] == ["city"]
    assert "additionalProperties" not in tool.parameters_schema


async def test_structured_fields_named_like_schema_keywords_are_preserved(agent, provider):
    class KeywordFields(BaseModel):
        default: str
        properties: str
        additional: str = Field(alias="additionalProperties")

    provider.return_value = completion(
        content='{"default":"value","properties":"text","additionalProperties":"extra"}'
    )
    result = await LLMService().generate_structured(agent, "Return keyword fields", KeywordFields)
    assert result.default == "value"
    schema = provider.await_args.kwargs["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"default", "properties", "additionalProperties"}
    assert set(schema["required"]) == set(schema["properties"])


async def test_stream_closes_resources_and_span_when_consumer_stops(agent, provider, monkeypatch):
    events = []

    @contextmanager
    def span(*_args, **_kwargs):
        events.append("span-enter")
        try:
            yield
        finally:
            events.append("span-exit")

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content="first"))
                ]
            )

        async def close(self):
            events.append("stream-close")

    provider.return_value = Stream()
    monkeypatch.setattr(llm_module, "safe_nova_span", span)
    stream = LLMService().stream_raw(agent, "Question")
    assert events == []
    assert await anext(stream) == "first"
    assert events == ["span-enter"]
    await stream.aclose()
    assert events == ["span-enter", "stream-close", "span-exit"]


async def test_client_shutdown_closes_once_and_resets_singleton(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(llm_module, "_llm_client", SimpleNamespace(close=close))
    await llm_module.close_llm_client()
    await llm_module.close_llm_client()
    close.assert_awaited_once()
    assert llm_module._llm_client is None
