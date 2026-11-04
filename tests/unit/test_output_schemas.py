"""Server-owned schemas stay versioned and frozen while workflows can resume."""

import copy
import uuid
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from src.workflows.output_schemas import (
    OutputSchemaError,
    OutputSchemaRef,
    OutputSchemaRegistry,
    ReviewDecisionV1,
)
from src.workflows.schemas import ExecutionLimits, WorkflowNodeSpec


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(OutputSchemaRegistry, "_definitions", {})
    return OutputSchemaRegistry


def register_score(registry, version=1):
    @registry.register("score", version, description="A trusted score contract")
    class Score(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
        value: int

    return Score


def node(schema_id="score", version=1, node_id="review"):
    return WorkflowNodeSpec(
        id=node_id,
        kind="llm_call",
        agent_id=uuid.uuid4(),
        output_schema={"id": schema_id, "version": version},
    )


def test_builtin_review_contract_is_catalogued_and_strict():
    entry = OutputSchemaRegistry.get({"id": "review_decision", "version": 1})
    assert entry.model is ReviewDecisionV1
    assert entry.json_schema["required"] == ["approved", "score"]
    assert entry.json_schema["additionalProperties"] is False
    assert ReviewDecisionV1.model_validate_json('{"approved":true,"score":0.87}').approved
    for value in (
        '{"approved":"true","score":0.87}',
        '{"approved":true,"score":1.01}',
        '{"approved":true,"score":NaN}',
        '{"approved":true,"score":0.87,"extra":1}',
    ):
        with pytest.raises(ValidationError):
            ReviewDecisionV1.model_validate_json(value, strict=True)


@pytest.mark.parametrize(
    "reference",
    [
        {"id": "python.module.Class", "version": 1},
        {"id": "schema/name", "version": 1},
        {"id": "UPPER", "version": 1},
        {"id": "a" * 65, "version": 1},
        {"id": "score", "version": 0},
        {"id": "score", "version": 1_000_001},
        {"id": "score", "version": "1"},
        {"id": "score", "version": True},
        {"id": "score", "version": 1.0},
        {"id": "score", "version": 1, "json_schema": {}},
    ],
)
def test_reference_accepts_only_bounded_server_identity(reference):
    with pytest.raises(ValidationError):
        OutputSchemaRef.model_validate(reference)


def test_catalog_and_get_schema_do_not_expose_mutable_registry_state(registry):
    model = register_score(registry)
    reference = OutputSchemaRef(id="score", version=1)
    first = registry.get(reference)
    exported = registry.list()
    exported[0]["json_schema"]["properties"]["value"]["type"] = "string"
    exported[0]["description"] = "changed"
    first.json_schema["properties"].clear()
    current = registry.get(reference)
    assert current.model is model
    assert current.json_schema["properties"]["value"]["type"] == "integer"
    assert current.description == "A trusted score contract"
    assert len(current.fingerprint) == 64
    assert set(registry.list()[0]) == {"id", "version", "description", "fingerprint", "json_schema"}


def test_duplicate_version_never_replaces_existing_model(registry):
    model = register_score(registry)
    with pytest.raises(OutputSchemaError, match="already registered"):
        register_score(registry)
    assert registry.get({"id": "score", "version": 1}).model is model


def test_constructed_reference_cannot_bypass_strict_version_validation(registry):
    register_score(registry)
    forged = OutputSchemaRef.model_construct(id="score", version=True)
    with pytest.raises(OutputSchemaError, match="identity is invalid"):
        registry.get(forged)


@pytest.mark.parametrize("model", [dict, str, RootModel[int], RootModel[list[int]]])
def test_only_object_pydantic_models_can_be_registered(registry, model):
    with pytest.raises(OutputSchemaError):
        registry.register("invalid", 1)(model)
    assert registry.list() == []


def test_open_mappings_are_rejected_before_they_can_enter_the_catalog(registry):
    class OpenMapping(BaseModel):
        data: dict[str, str]

    class PermissiveObject(BaseModel):
        model_config = ConfigDict(extra="allow")
        value: int

    class Nested(BaseModel):
        payload: OpenMapping

    class PatternedMapping(BaseModel):
        data: dict[Annotated[str, Field(pattern="^x")], str]

    for model in (OpenMapping, PermissiveObject, Nested, PatternedMapping):
        with pytest.raises(OutputSchemaError, match="not supported by strict schemas"):
            registry.register("unusable", 1)(model)
    assert registry.list() == []


def test_snapshot_preserves_selected_version_when_a_new_version_is_added(registry):
    original = register_score(registry)
    workflow_node = node()
    frozen = registry.snapshot_for_nodes([workflow_node])
    newer = register_score(registry, version=2)
    assert newer is not original
    assert registry.resolve_snapshot(workflow_node, frozen) is original
    assert frozen["review"]["version"] == 1
    assert [value["version"] for value in registry.list()] == [1, 2]


def test_snapshot_and_catalog_are_deeply_independent(registry):
    register_score(registry)
    workflow_node = node()
    frozen = registry.snapshot_for_nodes([workflow_node])
    catalog = registry.list()[0]
    catalog["json_schema"]["properties"].clear()
    assert registry.resolve_snapshot(workflow_node, frozen)
    frozen["review"]["json_schema"]["properties"].clear()
    assert registry.get(workflow_node.output_schema).json_schema["properties"]
    with pytest.raises(OutputSchemaError, match="fingerprint"):
        registry.resolve_snapshot(workflow_node, frozen)


def test_missing_or_removed_versions_never_fall_back_to_latest(registry):
    register_score(registry)
    workflow_node = node()
    frozen = registry.snapshot_for_nodes([workflow_node])
    register_score(registry, version=2)
    del registry._definitions[("score", 1)]
    with pytest.raises(OutputSchemaError, match="unavailable"):
        registry.resolve_snapshot(workflow_node, frozen)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot.clear(),
        lambda snapshot: snapshot["review"].update(version=2),
        lambda snapshot: snapshot["review"].update(version=True),
        lambda snapshot: snapshot["review"].update(id="other"),
        lambda snapshot: snapshot["review"].update(fingerprint="0" * 64),
        lambda snapshot: snapshot["review"].update(extra=True),
        lambda snapshot: snapshot["review"].update(json_schema=[]),
    ],
)
def test_missing_or_inconsistent_frozen_contracts_are_rejected(registry, mutate):
    register_score(registry)
    workflow_node = node()
    frozen = registry.snapshot_for_nodes([workflow_node])
    mutate(frozen)
    with pytest.raises(OutputSchemaError):
        registry.resolve_snapshot(workflow_node, frozen)


def test_schema_drift_is_checked_against_current_model_not_cached_metadata(registry, monkeypatch):
    model = register_score(registry)
    workflow_node = node()
    frozen = registry.snapshot_for_nodes([workflow_node])
    changed = copy.deepcopy(model.model_json_schema())
    changed["properties"]["value"]["type"] = "string"
    monkeypatch.setattr(model, "model_json_schema", classmethod(lambda cls, **kwargs: changed))
    with pytest.raises(OutputSchemaError, match="has changed"):
        registry.resolve_snapshot(workflow_node, frozen)
    with pytest.raises(OutputSchemaError, match="has changed"):
        registry.snapshot_for_nodes([workflow_node])


def test_unknown_contract_is_rejected_when_creating_snapshots(registry):
    with pytest.raises(OutputSchemaError, match="unavailable"):
        registry.snapshot_for_nodes([node()])


def test_plain_nodes_need_no_output_contract(registry):
    plain = WorkflowNodeSpec(id="approval", kind="hitl_approval")
    assert registry.snapshot_for_nodes([plain]) == {}
    with pytest.raises(OutputSchemaError, match="required"):
        registry.resolve_snapshot(plain, {})


def test_oversized_schema_and_snapshot_sets_are_bounded(registry):
    class Large(BaseModel):
        value: str

        @classmethod
        def model_json_schema(cls, **kwargs: Any):
            return {
                "type": "object",
                "properties": {"value": {"type": "string", "description": "x" * 59_000}},
            }

    registry.register("large", 1)(Large)
    with pytest.raises(OutputSchemaError, match="snapshots exceed"):
        registry.snapshot_for_nodes([node("large", node_id=f"node_{i}") for i in range(20)])

    class TooLarge(BaseModel):
        value: str

        @classmethod
        def model_json_schema(cls, **kwargs: Any):
            return {"type": "object", "description": "x" * 64_000}

    with pytest.raises(OutputSchemaError, match="schema exceeds"):
        registry.register("too_large", 1)(TooLarge)


@pytest.mark.parametrize("kind", ["hitl_approval", "condition", "tool_exec"])
def test_output_contracts_are_only_allowed_on_llm_nodes(kind):
    values = {
        "id": "node",
        "kind": kind,
        "tool_id": uuid.uuid4(),
        "config": {"condition": {"path": "input.ready", "op": "exists"}},
        "output_schema": {"id": "review_decision", "version": 1},
    }
    with pytest.raises(ValidationError, match="Only llm_call"):
        WorkflowNodeSpec.model_validate(values)


@pytest.mark.parametrize("value", [-1, 6, "2", True, 1.5])
def test_output_repair_limit_is_a_strict_bounded_integer(value):
    with pytest.raises(ValidationError):
        ExecutionLimits(max_output_repairs=value)


def test_output_repair_limit_defaults_and_zero_policy():
    assert ExecutionLimits().max_output_repairs == 2
    assert ExecutionLimits(max_output_repairs=0).max_output_repairs == 0
