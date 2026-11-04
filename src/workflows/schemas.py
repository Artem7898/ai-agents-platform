"""Validated, provider-independent workflow definitions."""

import uuid
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import UUID4, BaseModel, ConfigDict, Field, StrictBool, model_validator

from .output_schemas import OutputSchemaRef

type WorkflowID = UUID4
type AgentID = UUID4
type ToolID = uuid.UUID


class NodeKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_EXEC = "tool_exec"
    CONDITION = "condition"
    HITL_APPROVAL = "hitl_approval"


class ConditionPredicate(BaseModel):
    """A bounded lookup/comparison, never a Python expression.

    ``path`` traverses dictionary keys with dots, for example
    ``input.approved`` or ``nodes.decide.result``. Missing paths compare false,
    except ``exists`` which explicitly checks presence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str = Field(min_length=1, max_length=256, pattern=r"^[\w-]+(?:\.[\w-]+)*$")
    op: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "truthy", "falsy", "exists"
    ] = "eq"
    value: Any = None

    @model_validator(mode="after")
    def validate_operand(self) -> Self:
        if self.op in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError("Membership conditions require a list value")
        return self


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_steps: int = Field(default=1000, ge=1, le=10000)
    max_duration_seconds: float = Field(default=300, gt=0, le=3600)
    max_tool_calls: int = Field(default=32, ge=0, le=1000)
    max_tool_repairs: int = Field(default=2, ge=0, le=5, strict=True)
    max_output_repairs: int = Field(default=2, ge=0, le=5, strict=True)
    max_tokens: int = Field(default=32000, ge=1, le=1000000)
    max_output_tokens: int = Field(default=1024, ge=1, le=32000)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300)


class WorkflowNodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    kind: NodeKind
    agent_id: AgentID | None = None
    tool_id: ToolID | None = None
    output_schema: OutputSchemaRef | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        if self.output_schema is not None and self.kind != NodeKind.LLM_CALL:
            raise ValueError("Only llm_call nodes can select an output schema")
        if self.kind == NodeKind.LLM_CALL and self.agent_id is None:
            raise ValueError("llm_call nodes require agent_id")
        if self.kind == NodeKind.TOOL_EXEC and self.tool_id is None:
            raise ValueError("tool_exec nodes require tool_id")
        if self.kind == NodeKind.CONDITION:
            ConditionPredicate.model_validate(self.config.get("condition"))
        return self


class WorkflowEdgeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    target: str
    condition: ConditionPredicate | StrictBool | None = None


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: WorkflowID = Field(default_factory=uuid.uuid4)
    name: str = Field(min_length=1, max_length=256)
    version: int = Field(default=1, ge=1)
    nodes: list[WorkflowNodeSpec] = Field(min_length=1, max_length=1000)
    edges: list[WorkflowEdgeSpec] = Field(default_factory=list, max_length=10000)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    output_node_id: str | None = None

    @model_validator(mode="after")
    def validate_dag(self) -> Self:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Workflow node IDs must be unique")
        incoming = {node_id: 0 for node_id in nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise ValueError("Every edge must reference existing nodes")
            if (edge.source, edge.target) in pairs:
                raise ValueError("Duplicate edges are not allowed")
            pairs.add((edge.source, edge.target))
            if isinstance(edge.condition, bool) and nodes[edge.source].kind != NodeKind.CONDITION:
                raise ValueError("Boolean edge conditions require a condition source node")
            incoming[edge.target] += 1
            outgoing[edge.source].append(edge.target)
        ready = [node_id for node_id, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            node_id = ready.pop()
            visited += 1
            for target in outgoing[node_id]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
        if visited != len(nodes):
            raise ValueError("Workflow graph must be acyclic")
        if self.output_node_id is not None and self.output_node_id not in nodes:
            raise ValueError("output_node_id must reference an existing node")
        return self
