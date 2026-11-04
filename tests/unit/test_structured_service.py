"""Structured responses keep validation, serialization and metering at one boundary."""

import json
import uuid
from datetime import date
from enum import Enum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_serializer,
    field_validator,
    model_serializer,
)
from pydantic_core import PydanticCustomError

import src.agents.llm_service as module
from src.agents.llm_service import LLMResponseError, LLMService, StructuredOutputValidationError
from src.agents.schemas import AgentSpec
from src.usage.contracts import InvocationContext
from src.usage.transport import MeteredCompletion


class Decision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    approved: bool
    score: float = Field(ge=0, le=1)


def completion(
    content='{"approved":true,"score":0.75}', *, calls=None, refusal=None, finish="stop"
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(content=content, tool_calls=calls, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=6, total_tokens=10),
    )


@pytest.fixture
def agent():
    return AgentSpec(name="Structured", model_name="model", system_prompt="Return a decision")


@pytest.fixture
def provider(monkeypatch):
    create = AsyncMock(return_value=completion())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(module, "get_llm_client", lambda: client)
    return create


async def test_real_sdk_request_has_native_schema_without_tools_and_preserves_metadata(
    agent, monkeypatch
):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-structured",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"approved":true,"score":0.75}',
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            },
        )

    client = AsyncOpenAI(
        api_key="test-only",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(module, "get_llm_client", lambda: client)
    try:
        response = await LLMService().generate_structured_response(agent, "Decide", Decision)
    finally:
        await client.close()
    assert isinstance(response.result, Decision)
    assert response.data == {"approved": True, "score": 0.75}
    assert response.usage.total_tokens == 10
    assert response.accounted_tokens is None and response.unknown_calls == 0
    assert len(requests) == 1 and "tools" not in requests[0]
    contract = requests[0]["response_format"]["json_schema"]
    assert contract["strict"] is True
    assert contract["schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "content",
    [
        "private-invalid-json",
        "[]",
        "null",
        '{"approved":"private-invalid-json","score":0.5}',
        '{"approved":true,"score":2}',
        '{"approved":true,"score":0.5,"private-invalid-json":"private-invalid-json"}',
    ],
)
async def test_schema_failures_are_safe_repairable_and_never_retried_locally(
    agent, provider, content
):
    provider.return_value = completion(content)
    with pytest.raises(StructuredOutputValidationError) as caught:
        await LLMService().generate_structured_response(agent, "Decide", Decision)
    error = caught.value
    assert error.code == "invalid_structured_output"
    assert error.usage.total_tokens == 10 and error.accounted_tokens is None
    assert error.issues and all(set(issue) == {"code", "path"} for issue in error.issues)
    assert "private-invalid-json" not in str(error)
    assert "private-invalid-json" not in error.feedback
    assert "private-invalid-json" not in json.dumps(error.issues)
    provider.assert_awaited_once()


async def test_failed_schema_preserves_all_metered_attempt_consumption(
    agent, provider, monkeypatch
):
    result = completion('{"approved":"invalid","score":0.5}')
    metered = AsyncMock(return_value=MeteredCompletion(result, 45, 1))
    monkeypatch.setattr(module, "metered_completion", metered)
    context = InvocationContext(uuid.uuid4(), "review", 2)
    with pytest.raises(StructuredOutputValidationError) as caught:
        await LLMService().generate_structured_response(
            agent, "Decide", Decision, invocation_context=context
        )
    assert caught.value.accounted_tokens == 45 and caught.value.unknown_calls == 1
    assert caught.value.usage.total_tokens == 10
    assert metered.await_args.kwargs["operation"] == "structured"
    assert metered.await_args.args[2] == context
    provider.assert_not_awaited()


async def test_missing_usage_stays_unknown_on_success(agent, provider):
    provider.return_value.usage = None
    response = await LLMService().generate_structured_response(agent, "Decide", Decision)
    assert response.usage.total_tokens is None
    assert response.usage.prompt_tokens is response.usage.completion_tokens is None


@pytest.mark.parametrize(
    "response",
    [
        completion(refusal="private-refusal"),
        completion(finish="length"),
        completion(finish="content_filter"),
        completion(calls=[{"function": {"name": "private-tool"}}]),
        completion(calls={}, finish="stop"),
        completion(calls=[], finish="tool_calls"),
        completion(content=None),
    ],
)
async def test_provider_and_envelope_failures_are_terminal(agent, provider, response):
    provider.return_value = response
    with pytest.raises(LLMResponseError) as caught:
        await LLMService().generate_structured_response(agent, "Decide", Decision)
    assert not isinstance(caught.value, StructuredOutputValidationError)
    assert "private-" not in str(caught.value)
    provider.assert_awaited_once()


async def test_oversized_content_is_terminal_without_echoing_values(agent, provider):
    provider.return_value = completion("private-" * 32_001)
    with pytest.raises(LLMResponseError, match="size limit") as caught:
        await LLMService().generate_structured_response(agent, "Decide", Decision)
    assert not isinstance(caught.value, StructuredOutputValidationError)
    assert "private-" not in str(caught.value)


async def test_non_object_target_is_rejected_before_the_provider(agent, provider):
    class ListResult(RootModel[list[str]]):
        pass

    with pytest.raises(ValueError, match="object root"):
        await LLMService().generate_structured_response(agent, "Decide", ListResult)
    provider.assert_not_awaited()


async def test_custom_serializer_cannot_replace_the_object_root(agent, provider):
    class Serialized(BaseModel):
        approved: bool

        @model_serializer
        def serialize(self):
            return "private-output"

    provider.return_value = completion('{"approved":true}')
    with pytest.raises(LLMResponseError, match="JSON object") as caught:
        await LLMService().generate_structured_response(agent, "Decide", Serialized)
    assert not isinstance(caught.value, StructuredOutputValidationError)
    assert "private-output" not in str(caught.value)


async def test_custom_serializer_cannot_change_a_declared_field_type(agent, provider):
    class Serialized(BaseModel):
        approved: bool

        @field_serializer("approved")
        def serialize(self, value):
            return "private-output"

    provider.return_value = completion('{"approved":true}')
    with pytest.raises(LLMResponseError, match="serialization contract") as caught:
        await LLMService().generate_structured_response(agent, "Decide", Serialized)
    assert not isinstance(caught.value, StructuredOutputValidationError)
    assert "private-output" not in str(caught.value)


@pytest.mark.parametrize("serialized", [float("nan"), float("inf"), "\ud800"])
async def test_serializer_invalid_json_values_are_terminal_contract_errors(
    agent, provider, serialized
):
    class Serialized(BaseModel):
        value: int

        @field_serializer("value")
        def serialize(self, value):
            return serialized

    provider.return_value = completion('{"value":1}')
    with pytest.raises(LLMResponseError) as caught:
        await LLMService().generate_structured_response(agent, "Decide", Serialized)
    assert not isinstance(caught.value, StructuredOutputValidationError)


async def test_non_idempotent_validator_is_a_terminal_serialization_contract_error(agent, provider):
    class Unstable(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def increment(cls, value):
            return value + 1

    provider.return_value = completion('{"value":1}')
    with pytest.raises(LLMResponseError, match="serialization contract"):
        await LLMService().generate_structured_response(agent, "Decide", Unstable)


async def test_serializer_exception_does_not_echo_provider_input(agent, provider):
    class Serialized(BaseModel):
        value: str

        @field_serializer("value")
        def serialize(self, value):
            raise ValueError(value)

    provider.return_value = completion('{"value":"private-output"}')
    with pytest.raises(LLMResponseError, match="could not be serialized") as caught:
        await LLMService().generate_structured_response(agent, "Decide", Serialized)
    assert "private-output" not in str(caught.value)


async def test_non_finite_output_is_never_returned_as_persistable_data(agent, provider):
    class LooseResult(BaseModel):
        value: Any

    provider.return_value = completion('{"value":NaN}')
    with pytest.raises(StructuredOutputValidationError) as caught:
        await LLMService().generate_structured_response(agent, "Decide", LooseResult)
    assert caught.value.usage.total_tokens == 10
    assert caught.value.issues == ({"code": "invalid_json_result", "path": []},)


async def test_custom_validation_error_codes_and_values_are_redacted(agent, provider):
    class Custom(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value):
            raise PydanticCustomError(value, "private-message", {"private": value})

    provider.return_value = completion('{"value":"private-marker"}')
    with pytest.raises(StructuredOutputValidationError) as caught:
        await LLMService().generate_structured_response(agent, "Decide", Custom)
    assert "private-" not in caught.value.feedback
    assert caught.value.issues == ({"code": "invalid_value", "path": ["value"]},)


async def test_unexpected_validator_errors_are_terminal_without_provider_values(agent, provider):
    class Custom(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value):
            raise TypeError(value)

    provider.return_value = completion('{"value":"private-marker"}')
    with pytest.raises(LLMResponseError, match="failed internally") as caught:
        await LLMService().generate_structured_response(agent, "Decide", Custom)
    assert not isinstance(caught.value, StructuredOutputValidationError)
    assert "private-" not in str(caught.value)


class Category(str, Enum):
    REVIEW = "review"


async def test_json_wire_types_and_aliases_survive_stable_roundtrip(agent, provider):
    class Nested(BaseModel):
        key: uuid.UUID
        at: date
        category: Category

    class Typed(BaseModel):
        nested: Nested = Field(alias="payload")

    key = uuid.uuid4()
    body = {"payload": {"key": str(key), "at": "2026-11-05", "category": "review"}}
    provider.return_value = completion(json.dumps(body))
    response = await LLMService().generate_structured_response(agent, "Decide", Typed)
    assert isinstance(response.result.nested, Nested)
    assert response.result.nested.key == key
    assert response.result.nested.at == date(2026, 11, 5)
    assert response.result.nested.category is Category.REVIEW
    assert response.data == body
