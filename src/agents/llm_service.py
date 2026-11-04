"""Provider transport, typed message mapping and bounded completion handling."""

import copy
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager, contextmanager
from typing import Any, TypeVar, cast
from uuid import UUID

import httpx
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionToolUnionParam
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.runtimes.clients import close_runtime_clients, runtime_client
from src.runtimes.schemas import RuntimeSpec
from src.runtimes.secrets import RuntimeConfigurationError
from src.usage.contracts import InvocationContext
from src.usage.transport import metered_completion, metered_stream, normalized_usage

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
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    accounted_tokens: int | None = Field(default=None, ge=0)
    unknown_calls: int = Field(default=0, ge=0)

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
    try:
        if _llm_client is not None:
            client, _llm_client = _llm_client, None
            await client.close()
    finally:
        await close_runtime_clients()


def _validate_runtime(agent: AgentSpec, runtime: RuntimeSpec | None) -> None:
    if runtime is None:
        if agent.runtime_id is not None:
            raise RuntimeConfigurationError("Agent requires its assigned runtime")
        return
    if agent.runtime_id is None or str(agent.runtime_id) != str(runtime.id):
        raise RuntimeConfigurationError("Agent runtime does not match its assigned runtime")
    if not runtime.enabled:
        raise RuntimeConfigurationError("Runtime is disabled")
    if agent.model_name not in runtime.allowed_models:
        raise RuntimeConfigurationError("Model is not allowed by the assigned runtime")


@asynccontextmanager
async def _provider_client(agent: AgentSpec, runtime: RuntimeSpec | None):
    _validate_runtime(agent, runtime)
    if runtime is None:
        yield get_llm_client()
        return
    try:
        async with runtime_client(runtime) as client:
            yield client
    except OpenAIError as exc:
        # Vendor bodies/headers may echo credentials. Persist only a safe status.
        status = getattr(exc, "status_code", None)
        suffix = f" (HTTP {status})" if type(status) is int else ""
        raise LLMResponseError(f"Runtime provider request failed{suffix}") from None
    except ValidationError:
        raise LLMResponseError("Runtime provider returned invalid typed data") from None


def _completion_parameters(
    agent: AgentSpec, runtime: RuntimeSpec | None, max_output_tokens: int
) -> dict[str, Any]:
    _validate_runtime(agent, runtime)
    parameters: dict[str, Any] = {"model": agent.model_name}
    token_parameter = (
        runtime.capabilities.token_limit_parameter if runtime else "max_completion_tokens"
    )
    parameters[token_parameter] = max_output_tokens
    if runtime is None or runtime.capabilities.temperature:
        parameters["temperature"] = agent.temperature
    return parameters


def _require_capability(runtime: RuntimeSpec | None, name: str) -> None:
    if runtime is not None and not getattr(runtime.capabilities, name):
        raise RuntimeConfigurationError(f"Runtime does not support {name}")


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


def _map_tools_to_openai_spec(
    tools: Sequence[ToolSpec], *, strict: bool = True
) -> list[ChatCompletionToolUnionParam]:
    mapped = []
    for tool in tools:
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": (
                _strict_json_schema(tool.parameters_schema)
                if strict
                else copy.deepcopy(tool.parameters_schema)
            ),
        }
        if strict:
            function["strict"] = True
        mapped.append(
            {
                "type": "function",
                "function": function,
            }
        )
    return cast(list[ChatCompletionToolUnionParam], mapped)


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
        raise LLMResponseError("Provider completion did not finish successfully")
    message = choice.message
    if getattr(message, "refusal", None):
        raise LLMResponseError("Provider refused the request")
    return message


def _usage(response: Any) -> TokenUsage:
    return TokenUsage(**(normalized_usage(response) or {}))


async def _create_completion(
    client: AsyncOpenAI,
    parameters: dict[str, Any],
    runtime: RuntimeSpec | None,
    invocation_context: InvocationContext | None,
    max_output_tokens: int,
    operation: str,
) -> tuple[Any, int | None, int]:
    if invocation_context is None:
        return await client.chat.completions.create(**parameters), None, 0
    result = await metered_completion(
        client,
        parameters,
        invocation_context,
        runtime_id=UUID(runtime.id) if runtime else None,
        provider=runtime.profile if runtime else "openai",
        max_output_tokens=max_output_tokens,
        operation=operation,
    )
    return result.response, result.accounted_tokens, result.unknown_calls


class LLMService:
    """Direct calls remain unmetered; platform workflow execution supplies context."""

    async def generate_with_tools(
        self,
        agent_spec: AgentSpec,
        prompt: str = "",
        tools: list[ToolSpec] | None = None,
        context_history: list[ChatMessage | ContentBlock] | None = None,
        *,
        max_output_tokens: int = 1024,
        runtime: RuntimeSpec | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> LLMResponse:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        parameters = _completion_parameters(agent_spec, runtime, max_output_tokens)
        parameters["messages"] = messages
        if tools or any(
            message.get("tool_calls") or message["role"] == "tool" for message in messages
        ):
            _require_capability(runtime, "tools")
        if tools:
            parameters["tools"] = _map_tools_to_openai_spec(
                tools, strict=runtime is None or runtime.capabilities.strict_tools
            )
        with safe_nova_span("llm.generate_with_tools", model=agent_spec.model_name):
            async with _provider_client(agent_spec, runtime) as client:
                response, accounted, unknown = await _create_completion(
                    client, parameters, runtime, invocation_context, max_output_tokens, "tools"
                )
                message = _completion_message(response)
                calls = []
                for call in message.tool_calls or []:
                    try:
                        arguments = json.loads(call.function.arguments)
                        # JSON's wire format cannot carry NaN or infinite numbers.
                        json.dumps(arguments, allow_nan=False)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        raise LLMResponseError("Provider returned invalid tool arguments") from None
                    if not isinstance(arguments, dict):
                        raise LLMResponseError("Tool arguments must be a JSON object")
                    calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
                if message.content is None and not calls:
                    raise LLMResponseError("Provider returned an empty completion")
                return LLMResponse(
                    content=message.content,
                    tool_calls=calls,
                    usage=_usage(response),
                    accounted_tokens=accounted,
                    unknown_calls=unknown,
                )

    async def generate_structured(
        self,
        agent_spec: AgentSpec,
        prompt: str,
        target_schema: type[T],
        context_history: list[ChatMessage | ContentBlock] | None = None,
        *,
        max_output_tokens: int = 1024,
        runtime: RuntimeSpec | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> T:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        parameters = _completion_parameters(agent_spec, runtime, max_output_tokens)
        _require_capability(runtime, "structured_outputs")
        parameters["messages"] = messages
        schema = _strict_json_schema(target_schema.model_json_schema())
        parameters["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": target_schema.__name__,
                "strict": True,
                "schema": schema,
            },
        }
        with safe_nova_span("llm.generate_structured", model=agent_spec.model_name):
            async with _provider_client(agent_spec, runtime) as client:
                response, _, _ = await _create_completion(
                    client, parameters, runtime, invocation_context, max_output_tokens, "structured"
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
        runtime: RuntimeSpec | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> AsyncIterator[str]:
        _check_output_limit(max_output_tokens)
        messages = _build_messages(agent_spec, prompt, context_history)
        parameters = _completion_parameters(agent_spec, runtime, max_output_tokens)
        _require_capability(runtime, "streaming")
        parameters.update(messages=messages, stream=True)
        if (runtime is not None and runtime.capabilities.usage) or (
            runtime is None and invocation_context is not None
        ):
            parameters["stream_options"] = {"include_usage": True}
        with safe_nova_span("llm.stream_raw", model=agent_spec.model_name):
            async with _provider_client(agent_spec, runtime) as client:
                if invocation_context is not None:
                    chunks = metered_stream(
                        client,
                        parameters,
                        invocation_context,
                        runtime_id=UUID(runtime.id) if runtime else None,
                        provider=runtime.profile if runtime else "openai",
                        max_output_tokens=max_output_tokens,
                    )
                    async with aclosing(chunks):
                        async for chunk in chunks:
                            text = _stream_content(chunk)
                            if text:
                                yield text
                    return
                stream = await client.chat.completions.create(**parameters)
                try:
                    async for chunk in stream:
                        text = _stream_content(chunk)
                        if text:
                            yield text
                finally:
                    await stream.close()


def _stream_content(chunk: Any) -> str | None:
    if not chunk.choices:
        return None
    choice = chunk.choices[0]
    if choice.finish_reason in ("length", "content_filter"):
        raise LLMResponseError(f"Stream did not finish: {choice.finish_reason}")
    return choice.delta.content
