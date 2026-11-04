"""Typed tool registration and permission checks at the execution boundary."""

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from typing import Any, TypeAliasType, get_args, get_type_hints

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from pydantic_core.core_schema import ErrorType

from .schemas import ToolCall, ToolSpec
from .tool_context import ToolExecutionContext

_TOOL_NAMESPACE = uuid.UUID("9f2b24fe-fbe1-4ef9-8d8a-196eb378a659")
_execution_key: ContextVar[str | None] = ContextVar("tool_execution_key", default=None)
_VALIDATION_CODES = frozenset(get_args(ErrorType))
_MAX_ARGUMENT_BYTES = 256_000


class ToolArgumentsValidationError(ValueError):
    """Repairable argument errors; messages never include input or validator text."""

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = tuple(issues[:64])
        self.feedback = (
            "Tool arguments failed validation. Correct the arguments to match the tool schema: "
            + json.dumps(self.issues[:8], ensure_ascii=True, separators=(",", ":"))
        )[:2048]
        super().__init__("Tool arguments failed schema validation")


def _schema_fields(schema: dict[str, Any]) -> set[str]:
    names = set()

    def visit(value):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return names


def _safe_issues(error: ValidationError, schema: dict[str, Any]) -> list[dict[str, Any]]:
    fields = _schema_fields(schema)
    issues = []
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:64]:
        code = item.get("type")
        code = code if code in _VALIDATION_CODES else "invalid_value"
        path = []
        length = 0
        for part in item.get("loc", ())[:8]:
            if isinstance(part, str):
                safe = (
                    part
                    if part in fields and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", part)
                    else "[field]"
                )
            elif type(part) is int and 0 <= part <= 999999:
                safe = part
            else:
                safe = "[field]"
            if length + len(str(safe)) + 1 > 128:
                break
            path.append(safe)
            length += len(str(safe)) + 1
        issues.append({"code": code, "path": path})
    return issues


def _contains_context(annotation: Any, seen: set[int] | None = None) -> bool:
    """A wrapped dependency must never become an ordinary deserializable field."""
    if annotation is ToolExecutionContext:
        return True
    seen = set() if seen is None else seen
    identity = id(annotation)
    if identity in seen:
        return False
    seen.add(identity)
    if any(_contains_context(argument, seen) for argument in get_args(annotation)):
        return True
    # NewType and Python 3.12 type aliases may hide a deserializable dataclass.
    if hasattr(annotation, "__supertype__"):
        return _contains_context(annotation.__supertype__, seen)
    if isinstance(annotation, TypeAliasType):
        return _contains_context(annotation.__value__, seen)
    if isinstance(annotation, type):
        if issubclass(annotation, ToolExecutionContext):
            return True
        if issubclass(annotation, BaseModel):
            return any(
                _contains_context(field.annotation, seen)
                for field in annotation.model_fields.values()
            )
        if getattr(annotation, "__annotations__", None):
            try:
                annotations = get_type_hints(annotation, include_extras=True)
            except (TypeError, NameError) as exc:
                raise ValueError("Tool annotations must resolve before registration") from exc
            return any(_contains_context(value, seen) for value in annotations.values())
    return False


def current_tool_idempotency_key() -> str | None:
    """External adapters may forward this stable key to an idempotent backend."""
    return _execution_key.get()


class ToolRegistry:
    _executors: dict[str, Callable[..., Awaitable[Any]]] = {}
    _definitions: dict[str, ToolSpec] = {}
    _argument_models: dict[str, type[BaseModel]] = {}
    _context_parameters: dict[str, str | None] = {}

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
            context_parameter = None
            for parameter in signature.parameters.values():
                if parameter.kind not in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    raise ValueError("Tool arguments must have explicit named parameters")
                if parameter.name not in annotations:
                    raise ValueError(f"Tool argument '{parameter.name}' needs a type annotation")
                if annotations[parameter.name] is ToolExecutionContext:
                    if context_parameter is not None:
                        raise ValueError("Tools may declare only one workflow context dependency")
                    if parameter.default is not inspect.Parameter.empty:
                        raise ValueError("A workflow context dependency cannot have a default")
                    context_parameter = parameter.name
                    continue
                if _contains_context(annotations[parameter.name]):
                    raise ValueError(
                        "Workflow context requires an exact, unwrapped dependency annotation"
                    )
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                fields[parameter.name] = (annotations[parameter.name], default)
            argument_model = create_model(
                f"ToolArguments_{name}",
                __config__=ConfigDict(
                    strict=True, extra="forbid", allow_inf_nan=False, validate_default=True
                ),
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
            cls._context_parameters[name] = context_parameter
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
    def _check_allowed(cls, tool_name: str, allowed_tool_ids: Collection[uuid.UUID | str]) -> None:
        if tool_name not in cls._executors:
            raise ValueError("Requested tool was not found")
        if str(cls._definitions[tool_name].id) not in {str(value) for value in allowed_tool_ids}:
            raise PermissionError("Requested tool is not allowed")

    @classmethod
    def validate_arguments(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        allowed_tool_ids: Collection[uuid.UUID | str],
    ) -> BaseModel:
        """Validate without calling a tool or exposing model data in errors."""
        cls._check_allowed(tool_name, allowed_tool_ids)
        if not isinstance(arguments, dict):
            raise ToolArgumentsValidationError([{"code": "dict_type", "path": []}])
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ToolArgumentsValidationError([{"code": "json_invalid", "path": []}]) from None
        if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ToolArgumentsValidationError([{"code": "arguments_too_large", "path": []}])
        try:
            # JSON mode accepts legitimate wire forms such as UUID/date strings,
            # while strict scalar validation still rejects numeric strings.
            return cls._argument_models[tool_name].model_validate_json(
                encoded, strict=True, extra="forbid"
            )
        except ValidationError as exc:
            raise ToolArgumentsValidationError(
                _safe_issues(exc, cls._definitions[tool_name].parameters_schema)
            ) from None

    @classmethod
    def validate_calls(
        cls, calls: list[ToolCall], *, allowed_tool_ids: Collection[uuid.UUID | str]
    ) -> None:
        """Authorize the entire batch before repairing or executing any call."""
        if len(calls) > 1000:
            raise ValueError("Tool call batch exceeds the configured limit")
        ids = set()
        for call in calls:
            if len(call.id) > 128 or call.id in ids:
                raise ValueError("Tool call identifiers must be bounded and unique")
            ids.add(call.id)
            cls._check_allowed(call.name, allowed_tool_ids)
        for call in calls:
            cls.validate_arguments(call.name, call.arguments, allowed_tool_ids=allowed_tool_ids)

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
        execution_context: ToolExecutionContext | None = None,
    ) -> Any:
        cls._check_allowed(tool_name, allowed_tool_ids)
        if not 0 < timeout <= 300:
            raise ValueError("Tool timeout must be between 0 and 300 seconds")
        if max_result_bytes < 1:
            raise ValueError("Tool result limit must be positive")
        context_parameter = cls._context_parameters.get(tool_name)
        if context_parameter and not isinstance(execution_context, ToolExecutionContext):
            raise ValueError("This tool requires a workflow execution context")
        if execution_context is not None:
            if not isinstance(execution_context, ToolExecutionContext):
                raise ValueError("Invalid workflow execution context")
            if idempotency_key is not None and execution_context.idempotency_key != idempotency_key:
                raise ValueError("Workflow context and tool idempotency keys must agree")
            idempotency_key = execution_context.idempotency_key
        parsed = cls.validate_arguments(tool_name, arguments, allowed_tool_ids=allowed_tool_ids)
        # model_dump() would replace nested Pydantic instances with dictionaries.
        kwargs = {name: getattr(parsed, name) for name in type(parsed).model_fields}
        if context_parameter:
            kwargs[context_parameter] = execution_context
        token = _execution_key.set(idempotency_key)
        try:
            with trace.get_tracer(__name__).start_as_current_span("tool.execute") as span:
                span.set_attribute("tool.name", tool_name)
                async with asyncio.timeout(timeout):
                    result = await cls._executors[tool_name](**kwargs)
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
