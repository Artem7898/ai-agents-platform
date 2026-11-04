"""Tool boundaries must enforce capabilities and validate before calling Python."""

import asyncio
import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import date
from enum import Enum
from typing import Annotated, NewType, TypeAliasType, TypedDict
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, PydanticSchemaGenerationError, TypeAdapter, field_validator
from pydantic_core import PydanticCustomError

from src.agents.schemas import ToolCall
from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolArgumentsValidationError, ToolRegistry


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    for attribute in ("_executors", "_definitions", "_argument_models", "_context_parameters"):
        monkeypatch.setattr(ToolRegistry, attribute, {})


def register_add():
    @ToolRegistry.register(description="Add numbers")
    async def add(a: int, b: int = 3) -> int:
        return a + b

    return next(spec for spec in ToolRegistry.get_specs() if spec.name == "add")


def test_registered_tool_has_deterministic_id_and_numeric_schema(monkeypatch):
    spec = register_add()
    assert isinstance(spec.id, UUID)
    assert spec.id.version == 5
    assert spec.parameters_schema["properties"]["a"]["type"] == "integer"
    assert spec.parameters_schema["properties"]["b"]["type"] == "integer"
    assert spec.parameters_schema["additionalProperties"] is False
    assert ToolRegistry.get_spec_by_id(str(spec.id)).name == "add"

    # Simulate another process with an independently initialized registry.
    for attribute in ("_executors", "_definitions", "_argument_models", "_context_parameters"):
        monkeypatch.setattr(ToolRegistry, attribute, {})
    assert register_add().id == spec.id


async def test_duplicate_registration_is_rejected_without_replacing_original():
    original = register_add()

    @ToolRegistry.register(description="Original unique tool")
    async def unique_tool() -> str:
        return "original"

    async def replacement() -> str:
        return "replacement"

    replacement.__name__ = "unique_tool"
    with pytest.raises(ValueError):
        ToolRegistry.register(description="Replacement")(replacement)
    assert ToolRegistry.get_spec_by_id(original.id).name == "add"
    unique_spec = next(spec for spec in ToolRegistry.get_specs() if spec.name == "unique_tool")
    result = await ToolRegistry.execute("unique_tool", {}, allowed_tool_ids=[unique_spec.id])
    assert result == "original"


async def test_execution_applies_validated_default_arguments():
    spec = register_add()
    result = await ToolRegistry.execute("add", {"a": 5}, allowed_tool_ids=[str(spec.id)])
    assert result == 8


@pytest.mark.parametrize("arguments", [{"a": "5", "b": "3"}, {"b": 3}, {"a": 5, "extra": 1}])
async def test_invalid_arguments_are_rejected_before_the_function_runs(arguments):
    calls = []

    @ToolRegistry.register(description="Observe arguments")
    async def observed(a: int, b: int = 3) -> int:
        calls.append((a, b))
        return a + b

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ValueError):
        await ToolRegistry.execute("observed", arguments, allowed_tool_ids=[spec.id])
    assert calls == []


async def test_tool_is_denied_even_when_its_name_is_known():
    spec = register_add()
    with pytest.raises(PermissionError):
        await ToolRegistry.execute("add", {"a": 5}, allowed_tool_ids=[])
    assert ToolRegistry.get_specs(allowed_tool_ids=[]) == []
    assert ToolRegistry.get_specs(allowed_tool_ids=[str(spec.id)]) == [spec]


async def test_unknown_tool_is_rejected():
    with pytest.raises(ValueError, match="not found"):
        await ToolRegistry.execute("unknown", {}, allowed_tool_ids=[])


async def test_tool_deadline_cancels_an_unfinished_coroutine():
    cancelled = asyncio.Event()

    @ToolRegistry.register(description="Wait until cancelled")
    async def hanging() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(TimeoutError):
        await ToolRegistry.execute("hanging", {}, allowed_tool_ids=[spec.id], timeout=0.01)
    assert cancelled.is_set()


async def test_idempotency_keys_are_isolated_between_concurrent_calls():
    from src.agents.tool_registry import current_tool_idempotency_key

    @ToolRegistry.register(description="Read request identity")
    async def identity() -> str | None:
        await asyncio.sleep(0)
        return current_tool_idempotency_key()

    spec = ToolRegistry.get_specs()[0]
    results = await asyncio.gather(
        ToolRegistry.execute("identity", {}, allowed_tool_ids=[spec.id], idempotency_key="run-a"),
        ToolRegistry.execute("identity", {}, allowed_tool_ids=[spec.id], idempotency_key="run-b"),
    )
    assert results == ["run-a", "run-b"]
    assert current_tool_idempotency_key() is None


async def test_tool_output_size_is_bounded():
    @ToolRegistry.register(description="Large output")
    async def large_output() -> str:
        return "x" * 100

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ValueError, match="limit"):
        await ToolRegistry.execute(
            "large_output", {}, allowed_tool_ids=[spec.id], max_result_bytes=10
        )


def test_sync_tools_cannot_block_the_event_loop():
    def sync_tool(query: str) -> str:
        return query

    with pytest.raises(ValueError, match="async"):
        ToolRegistry.register(description="Blocking function")(sync_tool)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_arguments_are_rejected_before_dispatch(value):
    calls = []

    @ToolRegistry.register(description="Measure a finite value")
    async def measure(amount: float) -> float:
        calls.append(amount)
        return amount

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ValueError):
        await ToolRegistry.execute("measure", {"amount": value}, allowed_tool_ids=[spec.id])
    assert calls == []


def test_pure_validation_applies_defaults_without_executing_any_call():
    calls = []

    @ToolRegistry.register(description="Observe pure validation")
    async def observe(value: int, multiplier: int = 2) -> int:
        calls.append(value)
        return value * multiplier

    spec = ToolRegistry.get_specs()[0]
    validated = ToolRegistry.validate_arguments("observe", {"value": 4}, allowed_tool_ids=[spec.id])
    ToolRegistry.validate_calls(
        [ToolCall(id="call-1", name="observe", arguments={"value": 4})],
        allowed_tool_ids=[spec.id],
    )
    assert validated.value == 4
    assert validated.multiplier == 2
    assert calls == []


def test_invalid_batch_does_not_execute_preceding_valid_call():
    calls = []

    @ToolRegistry.register(description="Observe batch")
    async def observe(value: int) -> int:
        calls.append(value)
        return value

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ToolArgumentsValidationError):
        ToolRegistry.validate_calls(
            [
                ToolCall(id="valid", name="observe", arguments={"value": 1}),
                ToolCall(id="invalid", name="observe", arguments={"value": "2"}),
            ],
            allowed_tool_ids=[spec.id],
        )
    assert calls == []


def test_batch_authorization_precedes_argument_validation():
    spec = register_add()

    @ToolRegistry.register(description="Forbidden tool")
    async def forbidden() -> None:
        pass

    with pytest.raises(PermissionError):
        ToolRegistry.validate_calls(
            [
                ToolCall(id="bad-args", name="add", arguments={"a": "invalid"}),
                ToolCall(id="denied", name="forbidden", arguments={}),
            ],
            allowed_tool_ids=[spec.id],
        )


def test_batch_rejects_duplicate_ids_before_dispatch():
    spec = register_add()
    call = ToolCall(id="duplicate", name="add", arguments={"a": 1})
    with pytest.raises(ValueError, match="unique"):
        ToolRegistry.validate_calls([call, call], allowed_tool_ids=[spec.id])


@pytest.mark.parametrize("count", [129, 2000])
def test_batch_rejects_unbounded_ids(count):
    spec = register_add()
    with pytest.raises(ValueError, match="bounded"):
        ToolRegistry.validate_calls(
            [ToolCall(id="x" * count, name="add", arguments={"a": 1})],
            allowed_tool_ids=[spec.id],
        )


def test_validation_feedback_redacts_unknown_field_names_and_input():
    spec = register_add()
    secret = "secret-extra-field-and-input"
    with pytest.raises(ToolArgumentsValidationError) as error:
        ToolRegistry.validate_arguments(
            "add", {"a": secret, secret: secret}, allowed_tool_ids=[spec.id]
        )
    assert secret not in str(error.value)
    assert secret not in error.value.feedback
    assert secret not in json.dumps(error.value.issues)
    assert {issue["code"] for issue in error.value.issues} == {"int_type", "extra_forbidden"}
    assert any(issue["path"] == ["[field]"] for issue in error.value.issues)


def test_validation_feedback_ignores_custom_validator_messages_and_codes():
    class Payload(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value):
            raise PydanticCustomError(
                "secret-custom-code", "Secret custom value: {value}", {"value": value}
            )

    @ToolRegistry.register(description="Validate sensitive payload")
    async def sensitive(payload: Payload) -> None:
        pass

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ToolArgumentsValidationError) as error:
        ToolRegistry.validate_arguments(
            "sensitive", {"payload": {"value": "secret-user-value"}}, allowed_tool_ids=[spec.id]
        )
    assert "secret" not in error.value.feedback.lower()
    assert error.value.issues == ({"code": "invalid_value", "path": ["payload", "value"]},)


async def test_json_wire_validation_preserves_uuid_date_enum_and_nested_model():
    class Color(str, Enum):
        BLUE = "blue"

    class Payload(BaseModel):
        identifier: UUID
        date: date
        color: Color

    captured = []

    @ToolRegistry.register(description="Read typed data")
    async def typed(payload: Payload) -> str:
        captured.append(payload)
        return payload.color.value

    spec = ToolRegistry.get_specs()[0]
    identifier = uuid4()
    result = await ToolRegistry.execute(
        "typed",
        {"payload": {"identifier": str(identifier), "date": "2026-11-05", "color": "blue"}},
        allowed_tool_ids=[spec.id],
    )
    assert result == "blue"
    assert isinstance(captured[0], Payload)
    assert captured[0].identifier == identifier
    assert captured[0].date == date(2026, 11, 5)
    assert captured[0].color is Color.BLUE


def test_nested_model_extras_are_rejected_even_with_default_pydantic_config():
    class Payload(BaseModel):
        value: int

    @ToolRegistry.register(description="Read nested data")
    async def nested(payload: Payload) -> int:
        return payload.value

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ToolArgumentsValidationError):
        ToolRegistry.validate_arguments(
            "nested", {"payload": {"value": 1, "unexpected": 2}}, allowed_tool_ids=[spec.id]
        )


def execution_context(**kwargs):
    return ToolExecutionContext(
        run_id=uuid4(),
        workflow_id=uuid4(),
        node_id="node",
        tool_call_id="call",
        owner_id=1,
        idempotency_key="run:node:call",
        **kwargs,
    )


async def test_context_dependency_is_server_supplied_and_immutable():
    observed = []

    @ToolRegistry.register(description="Read workflow identity")
    async def scoped(value: int, *, context: ToolExecutionContext) -> str:
        observed.append((value, context))
        return str(context.run_id)

    spec = ToolRegistry.get_specs()[0]
    assert set(spec.parameters_schema["properties"]) == {"value"}
    assert "context" not in json.dumps(spec.parameters_schema)
    context = execution_context()
    result = await ToolRegistry.execute(
        "scoped", {"value": 4}, allowed_tool_ids=[spec.id], execution_context=context
    )
    assert result == str(context.run_id)
    assert observed == [(4, context)]
    with pytest.raises(FrozenInstanceError):
        context.node_id = "other"


async def test_context_cannot_be_supplied_as_model_arguments():
    observed = []

    @ToolRegistry.register(description="Read workflow identity")
    async def scoped(*, context: ToolExecutionContext) -> None:
        observed.append(context)

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ValueError, match="requires a workflow"):
        await ToolRegistry.execute("scoped", {}, allowed_tool_ids=[spec.id])
    with pytest.raises(ToolArgumentsValidationError):
        await ToolRegistry.execute(
            "scoped",
            {"context": {"owner_id": 99}},
            allowed_tool_ids=[spec.id],
            execution_context=execution_context(),
        )
    assert observed == []


async def test_context_dependency_still_requires_tool_allowlist_and_matching_key():
    @ToolRegistry.register(description="Read scoped data")
    async def scoped(context: ToolExecutionContext) -> str:
        return context.idempotency_key

    spec = ToolRegistry.get_specs()[0]
    context = execution_context()
    with pytest.raises(PermissionError):
        await ToolRegistry.execute("scoped", {}, allowed_tool_ids=[], execution_context=context)
    with pytest.raises(ValueError, match="keys must agree"):
        await ToolRegistry.execute(
            "scoped",
            {},
            allowed_tool_ids=[spec.id],
            execution_context=context,
            idempotency_key="other",
        )


def test_multiple_context_dependencies_and_default_context_are_rejected():
    async def twice(one: ToolExecutionContext, two: ToolExecutionContext) -> None:
        pass

    with pytest.raises(ValueError, match="only one"):
        ToolRegistry.register(description="Two dependencies")(twice)
    context = execution_context()

    async def defaulted(context: ToolExecutionContext = context) -> None:
        pass

    with pytest.raises(ValueError, match="default"):
        ToolRegistry.register(description="Default dependency")(defaulted)


@pytest.mark.parametrize(
    "annotation",
    [
        ToolExecutionContext | None,
        Annotated[ToolExecutionContext, "dependency"],
        list[ToolExecutionContext],
        dict[str, ToolExecutionContext],
        NewType("ContextWrapper", ToolExecutionContext),
        TypeAliasType("ContextAlias", ToolExecutionContext),
    ],
)
def test_wrapped_context_annotations_cannot_become_model_arguments(annotation):
    async def wrapped(payload: annotation) -> None:
        pass

    with pytest.raises(ValueError, match="unwrapped"):
        ToolRegistry.register(description="Wrapped workflow context")(wrapped)
    assert ToolRegistry.get_specs() == []


def test_nested_model_context_and_dataclass_context_are_rejected():
    with pytest.raises(PydanticSchemaGenerationError, match="cannot be a model argument"):

        class Payload(BaseModel):
            context: ToolExecutionContext

    @dataclass
    class Wrapper:
        context: ToolExecutionContext

    class DictionaryWrapper(TypedDict):
        context: ToolExecutionContext

    for annotation in (Wrapper, DictionaryWrapper):

        async def nested(payload: annotation) -> None:
            pass

        with pytest.raises(ValueError, match="unwrapped"):
            ToolRegistry.register(description="Nested context")(nested)
    assert ToolRegistry.get_specs() == []


def test_context_subclass_cannot_be_deserialized_from_arguments():
    class SpecializedContext(ToolExecutionContext):
        pass

    async def specialized(payload: SpecializedContext) -> None:
        pass

    with pytest.raises(ValueError, match="unwrapped"):
        ToolRegistry.register(description="Context subclass")(specialized)
    with pytest.raises(PydanticSchemaGenerationError, match="cannot be a model argument"):
        TypeAdapter(SpecializedContext)
    with pytest.raises(PydanticSchemaGenerationError, match="cannot be a model argument"):
        TypeAdapter(ToolExecutionContext | None)


async def test_invalid_typed_default_never_reaches_tool_function():
    observed = []

    @ToolRegistry.register(description="Reject invalid backend defaults")
    async def invalid_default(value: int = "3") -> None:
        observed.append(value)

    spec = ToolRegistry.get_specs()[0]
    with pytest.raises(ToolArgumentsValidationError):
        await ToolRegistry.execute("invalid_default", {}, allowed_tool_ids=[spec.id])
    assert observed == []
