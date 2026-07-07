import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, UUID4

# PEP 695 Type Aliases
type AgentID = UUID4
type ToolID = UUID4
type WorkflowID = UUID4
type RunID = UUID4

class NodeKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_EXEC = "tool_exec"
    CONDITION = "condition"
    HITL_APPROVAL = "hitl_approval"

class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

# --- Content Blocks (Discriminated Union) ---
class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["text"] = "text"
    text: str = Field(..., description="Текстовое содержимое")

class ToolUseBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    tool_id: ToolID
    name: str
    arguments: dict[str, Any] = Field(..., description="Аргументы вызова инструмента")

class ToolResultBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False

type ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type")
]

# --- Core Specs ---
class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: ToolID = Field(default_factory=uuid.uuid4)
    name: str = Field(..., pattern=r"^[a-z0-9_]+$", description="Имя функции для LLM")
    description: str
    parameters_schema: dict[str, Any] = Field(..., description="JSON Schema аргументов")

class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: AgentID = Field(default_factory=uuid.uuid4)
    name: str
    model_name: str = Field(..., description="Например, gpt-4o, qwen-max")
    system_prompt: str
    tool_ids: list[ToolID] = Field(default_factory=list)
    temperature: float = Field(0.7, ge=0.0, le=2.0)

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

class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"

class WorkflowRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid") # Mutable state
    id: RunID = Field(default_factory=uuid.uuid4)
    workflow_id: WorkflowID
    status: RunStatus = RunStatus.PENDING
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)