import uuid
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, UUID4

type WorkflowID = UUID4
type AgentID = UUID4
type ToolID = UUID4

class NodeKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_EXEC = "tool_exec"
    CONDITION = "condition"
    HITL_APPROVAL = "hitl_approval"

class WorkflowNodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(..., description="Внутренний ID узла в DAG")
    kind: NodeKind
    agent_id: AgentID | None = None
    tool_id: ToolID | None = None
    config: dict[str, Any] = Field(default_factory=dict)

class WorkflowEdgeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    target: str
    condition: str | None = Field(None, description="Python-выражение или JSONPath для ветвления")

class WorkflowSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: WorkflowID = Field(default_factory=uuid.uuid4)
    name: str
    version: int = 1
    nodes: list[WorkflowNodeSpec]
    edges: list[WorkflowEdgeSpec]