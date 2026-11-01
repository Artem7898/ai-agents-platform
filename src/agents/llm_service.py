"""Provider transport, typed message mapping and bounded completion handling."""

import copy
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar, cast

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam
from openai.types.chat.completion_create_params import CompletionCreateParamsNonStreaming
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    AgentSpec,
    ChatMessage,
    ContentBlock,
    MessageRole,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

T = TypeVar("T", bound=BaseModel)
_llm_client: AsyncOpenAI | None = None


class LLMResponseError(ValueError):
    """The provider refused, truncated or returned an invalid completion."""


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)

    def as_message(self) -> ChatMessage:
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content=self.content,
            tool_calls=self.tool_calls,
        )


def get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        from django.conf import settings

        api_key = getattr(settings, "OPENAI_API_KEY", None) if settings.configured else None
        base_url = getattr(settings, "OPENAI_BASE_URL", None) if settings.configured else None
        _llm_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0,
            max_retries=2,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                timeout=60.0,
            ),
        )
    return _llm_client


async def close_llm_client() -> None:
    """Called by ASGI lifespan and worker shutdown on their owning event loop."""
    global _llm_client
    if _llm_client is not None:
        client, _llm_client = _llm_client, None
        await client.close()


@contextmanager
def safe_nova_span(name: str, **attributes: str | int | float | bool):
    """Use inside the awaited operation, never as a sync coroutine decorator."""
    with trace.get_tracer(__name__).start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        yield span


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic's object schemas to the provider's strict subset.

    Open-ended mappings cannot be represented in that subset and fail before a
    provider request. Defaults stay in the domain model, not the wire schema.
    """
    result = copy.deepcopy(schema)

    def visit(value: dict[str, Any]) -> None:
        # Schema containers such as `properties` are maps of user-chosen names;
        # process their values rather than treating the container as a schema.
        value.pop("default", None)
        if value.get("type") == "object" or "properties" in value:
            extra = value.get("additionalProperties", False)
            if extra is not False:
                raise ValueError("Strict output schemas cannot contain open-ended mappings")
            properties = value.setdefault("properties", {})
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for key in ("properties", "$defs", "definitions"):
            for child in value.get(key, {}).values():
                if isinstance(child, dict):
                    visit(child)
        for key in ("items", "not", "contains"):
            child = value.get(key)
            if isinstance(child, dict):
                visit(child)
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in value.get(key, []):
                if isinstance(child, dict):
                    visit(child)

    visit(result)
    return result


def _map_tools_to_openai_spec(tools: Sequence[ToolSpec]) -> list[ChatCompletionToolUnionParam]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _strict_json_schema(tool.parameters_schema),
                "strict": True,
            },
        }
        for tool in tools
    ]


def _map_history_to_openai_messages(
    history: Sequence[ChatMessage | ContentBlock],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for block in history:
        if isinstance(block, ChatMessage):
            message = block
        elif isinstance(block, TextBlock):
            message = ChatMessage(role=block.role, content=block.text)
        elif isinstance(block, ToolUseBlock):
            message = ChatMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    ToolCall(id=block.tool_use_id, name=block.name, arguments=block.arguments)
                ],
            )
        elif isinstance(block, ToolResultBlock):
            message = ChatMessage(
                role=MessageRole.TOOL,
                tool_call_id=block.tool_use_id,
                content=block.content,
            )
        else:
            raise TypeError("History entries must be ChatMessage or typed content blocks")
        mapped: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.tool_calls:
            mapped["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, allow_nan=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            mapped["tool_call_id"] = message.tool_call_id
        messages.append(mapped)
    return messages


def _build_messages(
    agent_spec: AgentSpec,
    prompt: str,
    history: Sequence[ChatMessage | ContentBlock] | None,
) -> list[dict[str, Any]]:
    if history is not None and len(history) > 200:
        raise ValueError("Conversation exceeds 200 messages")
    mapped = _map_history_to_openai_messages(history or [])
    if mapped and mapped[0]["role"] == "system":
        if mapped[0]["content"] != agent_spec.system_prompt:
            raise ValueError("History cannot override the agent system prompt")
        mapped = mapped[1:]
    if any(message["role"] == "system" for message in mapped):
        raise ValueError("System messages are only allowed at the start")
    messages = [{"role": "system", "content": agent_spec.system_prompt}, *mapped]
    if prompt or not mapped:
        messages.append({"role": "user", "content": prompt})
    if len(json.dumps(messages, ensure_ascii=False).encode()) > 1_000_000:
        raise ValueError("Conversation exceeds the request size limit")
    return messages


def _check_output_limit(max_output_tokens: int) -> None:
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32_768:
        raise ValueError("max_output_tokens must be between 1 and 32768")


def _completion_message(response: Any) -> Any:
    if not response.choices:
        raise LLMResponseError("Provider returned no completion choices")
    choice = response.choices[0]
    if choice.finish_reason not in ("stop", "tool_calls"):
        raise LLMResponseError(f"Completion did not finish: {choice.finish_reason}")
    message = choice.message
    if getattr(message, "refusal", None):
        raise LLMResponseError("Provider refused the request")
    return message


def _usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


class LLMService:
    async def generate_with_tools(
        self,
        agent_spec: AgentSpec,
        prompt: str = "",
        tools: list[ToolSpec] | None = None,
        context_history: list[ChatMessage | ContentBlock] | None = None,
        *,
        max_output_tokens: int = 1024,
    ) -> LLMResponse:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        parameters: CompletionCreateParamsNonStreaming = {
            "model": agent_spec.model_name,
            "messages": cast(list[ChatCompletionMessageParam], messages),
            "temperature": agent_spec.temperature,
            "max_completion_tokens": max_output_tokens,
        }
        if tools:
            parameters["tools"] = _map_tools_to_openai_spec(tools)
        with safe_nova_span("llm.generate_with_tools", model=agent_spec.model_name):
            response = await get_llm_client().chat.completions.create(**parameters)
            message = _completion_message(response)
            calls = []
            for call in message.tool_calls or []:
                try:
                    arguments = json.loads(call.function.arguments)
                    # JSON's wire format cannot carry NaN or infinite numbers.
                    json.dumps(arguments, allow_nan=False)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise LLMResponseError("Provider returned invalid tool arguments") from exc
                if not isinstance(arguments, dict):
                    raise LLMResponseError("Tool arguments must be a JSON object")
                calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
            if message.content is None and not calls:
                raise LLMResponseError("Provider returned an empty completion")
            return LLMResponse(content=message.content, tool_calls=calls, usage=_usage(response))

    async def generate_structured(
        self,
        agent_spec: AgentSpec,
        prompt: str,
        target_schema: type[T],
        context_history: list[ChatMessage | ContentBlock] | None = None,
        *,
        max_output_tokens: int = 1024,
    ) -> T:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        schema = _strict_json_schema(target_schema.model_json_schema())
        with safe_nova_span("llm.generate_structured", model=agent_spec.model_name):
            response = await get_llm_client().chat.completions.create(
                model=agent_spec.model_name,
                messages=cast(list[ChatCompletionMessageParam], messages),
                temperature=agent_spec.temperature,
                max_completion_tokens=max_output_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": target_schema.__name__,
                        "strict": True,
                        "schema": schema,
                    },
                },
            )
            content = _completion_message(response).content
            if content is None:
                raise LLMResponseError("Provider returned no structured content")
            return target_schema.model_validate_json(content, strict=True)

    async def stream_raw(
        self,
        agent_spec: AgentSpec,
        prompt: str,
        context_history: list[ChatMessage | ContentBlock] | None = None,
        *,
        max_output_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        with safe_nova_span("llm.stream_raw", model=agent_spec.model_name):
            stream = await get_llm_client().chat.completions.create(
                model=agent_spec.model_name,
                messages=cast(list[ChatCompletionMessageParam], messages),
                stream=True,
                temperature=agent_spec.temperature,
                max_completion_tokens=max_output_tokens,
            )
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason in ("length", "content_filter"):
                        raise LLMResponseError(f"Stream did not finish: {choice.finish_reason}")
                    if choice.delta.content:
                        yield choice.delta.content
            finally:
                await stream.close()
