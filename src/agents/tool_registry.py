"""Typed tool registration and permission checks at the execution boundary."""

import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from typing import Any, get_type_hints

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .schemas import ToolSpec

_TOOL_NAMESPACE = uuid.UUID("9f2b24fe-fbe1-4ef9-8d8a-196eb378a659")
_execution_key: ContextVar[str | None] = ContextVar("tool_execution_key", default=None)


def current_tool_idempotency_key() -> str | None:
    """External adapters may forward this stable key to an idempotent backend."""
    return _execution_key.get()


class ToolRegistry:
    _executors: dict[str, Callable[..., Awaitable[Any]]] = {}
    _definitions: dict[str, ToolSpec] = {}
    _argument_models: dict[str, type[BaseModel]] = {}

    @classmethod
    def register(cls, description: str):
        def decorator(func: Callable[..., Awaitable[Any]]):
            name = func.__name__
            if name in cls._executors:
                raise ValueError(f"Tool '{name}' is already registered")
            if not inspect.iscoroutinefunction(func):
                raise ValueError("Tools must be async functions")
            signature = inspect.signature(func)
            annotations = get_type_hints(func, include_extras=True)
            fields: dict[str, Any] = {}
            for parameter in signature.parameters.values():
                if parameter.kind not in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    raise ValueError("Tool arguments must have explicit named parameters")
                if parameter.name not in annotations:
                    raise ValueError(f"Tool argument '{parameter.name}' needs a type annotation")
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                fields[parameter.name] = (annotations[parameter.name], default)
            argument_model = create_model(
                f"ToolArguments_{name}",
                __config__=ConfigDict(strict=True, extra="forbid", allow_inf_nan=False),
                **fields,
            )
            definition = ToolSpec(
                id=uuid.uuid5(_TOOL_NAMESPACE, name),
                name=name,
                description=description,
                parameters_schema=argument_model.model_json_schema(),
            )
            cls._executors[name] = func
            cls._argument_models[name] = argument_model
            cls._definitions[name] = definition
            return func

        return decorator

    @classmethod
    def get_specs(
        cls, allowed_tool_ids: Collection[uuid.UUID | str] | None = None
    ) -> list[ToolSpec]:
        allowed = None if allowed_tool_ids is None else {str(value) for value in allowed_tool_ids}
        return [
            spec for spec in cls._definitions.values() if allowed is None or str(spec.id) in allowed
        ]

    @classmethod
    def get_spec_by_id(cls, tool_id: uuid.UUID | str) -> ToolSpec:
        for spec in cls._definitions.values():
            if str(spec.id) == str(tool_id):
                return spec
        raise ValueError(f"Tool '{tool_id}' not found")

    @classmethod
    async def execute(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        allowed_tool_ids: Collection[uuid.UUID | str],
        timeout: float = 30.0,
        idempotency_key: str | None = None,
        max_result_bytes: int = 256_000,
    ) -> Any:
        if tool_name not in cls._executors:
            raise ValueError(f"Tool '{tool_name}' not found")
        spec = cls._definitions[tool_name]
        if str(spec.id) not in {str(value) for value in allowed_tool_ids}:
            raise PermissionError(f"Tool '{tool_name}' is not allowed")
        if not 0 < timeout <= 300:
            raise ValueError("Tool timeout must be between 0 and 300 seconds")
        if max_result_bytes < 1:
            raise ValueError("Tool result limit must be positive")
        try:
            parsed = cls._argument_models[tool_name].model_validate(arguments)
        except ValidationError as exc:
            raise ValueError(f"Invalid arguments for {tool_name}: {exc}") from exc
        token = _execution_key.set(idempotency_key)
        try:
            with trace.get_tracer(__name__).start_as_current_span("tool.execute") as span:
                span.set_attribute("tool.name", tool_name)
                async with asyncio.timeout(timeout):
                    result = await cls._executors[tool_name](**parsed.model_dump())
                try:
                    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Tool '{tool_name}' returned a non-JSON result") from exc
                if len(encoded) > max_result_bytes:
                    raise ValueError(f"Tool '{tool_name}' result exceeds the configured limit")
                return result
        finally:
            _execution_key.reset(token)

    @classmethod
    def get_openai_tools_schema(
        cls, allowed_tool_ids: Collection[uuid.UUID | str] | None = None
    ) -> list[dict[str, Any]]:
        """Compatibility adapter; the engine should exchange domain ToolSpec values."""
        from .llm_service import _map_tools_to_openai_spec

        # Compatibility callers consume dictionaries rather than provider TypedDicts.
        return [
            dict(schema) for schema in _map_tools_to_openai_spec(cls.get_specs(allowed_tool_ids))
        ]
