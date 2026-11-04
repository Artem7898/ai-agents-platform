"""Invalid provider arguments are repairable data failures, not tool dispatches."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

import src.agents.llm_service as llm_module
from src.agents.llm_service import (
    LLMResponseError,
    LLMService,
    StructuredOutputValidationError,
    TokenUsage,
    ToolResponseValidationError,
)
from src.agents.schemas import AgentSpec, ToolSpec


def call(arguments="{}", *, identifier="call_1", name="safe_tool", kind="function"):
    return SimpleNamespace(
        id=identifier, type=kind, function=SimpleNamespace(name=name, arguments=arguments)
    )


def response(calls, *, usage=True):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=calls, refusal=None),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=6, total_tokens=10)
        if usage
        else None,
    )


@pytest.fixture
def agent():
    return AgentSpec(name="Typed", model_name="test", system_prompt="Use the permitted tools")


@pytest.fixture
def provider(monkeypatch):
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_module, "get_llm_client", lambda: client)
    return create


@pytest.fixture
def tools():
    return [
        ToolSpec(
            name="safe_tool",
            description="An allowed test tool",
            parameters_schema={"type": "object", "properties": {}},
        )
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        "PRIVATE-invalid-json",
        "[]",
        "null",
        '"PRIVATE-string"',
        '{"nested":[{"value":NaN}]}',
        '{"nested":{"value":Infinity}}',
        '{"nested":{"value":1e999}}',
        12,
    ],
)
async def test_repairable_errors_expose_only_safe_code_and_accounting(
    agent, provider, tools, arguments
):
    provider.return_value = response([call(arguments)])
    with pytest.raises(ToolResponseValidationError) as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    error = caught.value
    assert isinstance(error, LLMResponseError)
    assert error.code == "invalid_tool_arguments"
    assert error.usage == TokenUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10)
    assert error.accounted_tokens is None
    assert error.unknown_calls == 0
    assert str(error) == "Provider returned invalid tool arguments"
    assert "PRIVATE" not in repr(error)
    provider.assert_awaited_once()


async def test_repairable_metered_error_retains_all_attempt_accounting(agent, tools, monkeypatch):
    completed = AsyncMock(return_value=(response([call("invalid")], usage=False), 321, 2))
    monkeypatch.setattr(llm_module, "_create_completion", completed)
    monkeypatch.setattr(llm_module, "get_llm_client", lambda: SimpleNamespace())
    with pytest.raises(ToolResponseValidationError) as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert caught.value.accounted_tokens == 321
    assert caught.value.unknown_calls == 2
    assert caught.value.usage.total_tokens is None
    completed.assert_awaited_once()


@pytest.mark.parametrize("identifier", ["", "call\nPRIVATE", "x" * 129, None])
async def test_bad_call_metadata_is_fatal_and_does_not_echo_it(agent, provider, tools, identifier):
    provider.return_value = response([call("invalid", identifier=identifier)])
    with pytest.raises(LLMResponseError, match="metadata") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)
    assert "PRIVATE" not in str(caught.value)
    provider.assert_awaited_once()


@pytest.mark.parametrize("name", ["BAD_PRIVATE_NAME", "x" * 129, None])
async def test_bad_tool_name_is_fatal(agent, provider, tools, name):
    provider.return_value = response([call("invalid", name=name)])
    with pytest.raises(LLMResponseError, match="metadata") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_duplicate_ids_are_fatal_even_when_first_arguments_need_repair(
    agent, provider, tools
):
    provider.return_value = response([call("invalid"), call("{}")])
    with pytest.raises(LLMResponseError, match="duplicate") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)
    provider.assert_awaited_once()


@pytest.mark.parametrize("kind", [None, "custom", "function;PRIVATE"])
async def test_non_function_call_is_fatal_before_invalid_arguments(agent, provider, tools, kind):
    provider.return_value = response([call("invalid", kind=kind)])
    with pytest.raises(LLMResponseError, match="metadata") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)
    assert "PRIVATE" not in str(caught.value)
    provider.assert_awaited_once()


async def test_actual_sdk_loose_response_type_cannot_bypass_function_boundary(
    agent, tools, monkeypatch
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "custom",
                                    "function": {"name": "safe_tool", "arguments": "invalid"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://provider.example/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        monkeypatch.setattr(llm_module, "get_llm_client", lambda: client)
        with pytest.raises(LLMResponseError, match="metadata") as caught:
            await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)
    assert len(requests) == 1


async def test_invalid_completion_content_cannot_leak_through_pydantic_errors(agent, provider):
    provider.return_value = response([])
    provider.return_value.choices[0].message.content = {"private": "NEVER_PERSIST_THIS"}
    with pytest.raises(LLMResponseError, match="invalid completion content") as caught:
        await LLMService().generate_with_tools(agent, tools=[])
    assert not isinstance(caught.value, ToolResponseValidationError)
    assert "NEVER_PERSIST_THIS" not in str(caught.value)
    provider.assert_awaited_once()


@pytest.mark.parametrize("raw_calls", [{}, "", False, 0])
async def test_falsey_invalid_tool_call_list_is_not_treated_as_absent(agent, provider, raw_calls):
    provider.return_value = response(raw_calls)
    provider.return_value.choices[0].message.content = "Otherwise valid answer"
    with pytest.raises(LLMResponseError, match="invalid tool call list") as caught:
        await LLMService().generate_with_tools(agent, tools=[])
    assert not isinstance(caught.value, ToolResponseValidationError)
    provider.assert_awaited_once()


async def test_forbidden_tool_is_fatal_before_another_calls_invalid_json(agent, provider, tools):
    provider.return_value = response(
        [call("invalid"), call("{}", identifier="call_2", name="forbidden_tool")]
    )
    with pytest.raises(LLMResponseError, match="not allowed") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_explicit_empty_allowlist_rejects_all_calls(agent, provider):
    provider.return_value = response([call("invalid")])
    with pytest.raises(LLMResponseError, match="not allowed") as caught:
        await LLMService().generate_with_tools(agent, tools=[])
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_valid_batch_is_returned_whole_with_typed_arguments(agent, provider, tools):
    provider.return_value = response(
        [call('{"value":3}'), call('{"nested":[true,null]}', identifier="call_2")]
    )
    result = await LLMService().generate_with_tools(agent, tools=tools)
    assert [item.arguments for item in result.tool_calls] == [
        {"value": 3},
        {"nested": [True, None]},
    ]


async def test_single_argument_size_limit_is_fatal(agent, provider, tools):
    provider.return_value = response([call('{"value":"' + "x" * 256_000 + '"}')])
    with pytest.raises(LLMResponseError, match="size limit") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_batch_argument_size_limit_is_fatal(agent, provider, tools):
    provider.return_value = response(
        [call('{"value":"' + "x" * 210_000 + '"}', identifier=f"call_{i}") for i in range(5)]
    )
    with pytest.raises(LLMResponseError, match="size limit") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_call_count_limit_is_fatal(agent, provider, tools):
    provider.return_value = response([call(identifier=f"call_{i}") for i in range(129)])
    with pytest.raises(LLMResponseError, match="too many") as caught:
        await LLMService().generate_with_tools(agent, tools=tools)
    assert not isinstance(caught.value, ToolResponseValidationError)


async def test_invalid_structured_completion_is_validated_locally_without_hidden_retry(
    agent, provider
):
    class Score(BaseModel):
        score: int = Field(le=10)

    provider.return_value = response([])
    provider.return_value.choices[0].finish_reason = "stop"
    provider.return_value.choices[0].message.content = '{"score":11}'
    with pytest.raises(StructuredOutputValidationError):
        await LLMService().generate_structured(agent, "Score", Score)
    provider.assert_awaited_once()
    assert provider.await_args.kwargs["response_format"]["type"] == "json_schema"
