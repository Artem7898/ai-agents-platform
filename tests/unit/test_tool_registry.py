"""Tool boundaries must enforce capabilities and validate before calling Python."""

import asyncio
from uuid import UUID

import pytest

from src.agents.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    for attribute in ("_executors", "_definitions", "_argument_models"):
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
    for attribute in ("_executors", "_definitions", "_argument_models"):
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
