"""Domain contracts shared by the engine, tools and provider adapters."""

import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

type AgentID = UUID4
# Tool identities are stable UUID5 values derived from their registered name.
type ToolID = uuid.UUID


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)
    name: str = Field(pattern=r"^[a-z0-9_]+$", min_length=1)
    arguments: dict[str, Any]


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: MessageRole
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> "ChatMessage":
        if self.tool_calls and self.role != MessageRole.ASSISTANT:
            raise ValueError("Only assistant messages can request tools")
        if self.role == MessageRole.TOOL:
            if not self.tool_call_id or self.content is None:
                raise ValueError("Tool messages require tool_call_id and content")
        elif self.tool_call_id is not None:
            raise ValueError("Only tool messages can contain tool_call_id")
        if self.content is None and not self.tool_calls:
            raise ValueError("A message needs content or tool calls")
        return self


class TextBlock(BaseModel):
    """Compatibility input; new integrations should use ChatMessage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["text"] = "text"
    text: str
    role: MessageRole = MessageRole.ASSISTANT


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    tool_id: ToolID | None = None
    tool_use_id: str = Field(min_length=1)
    name: str
    arguments: dict[str, Any]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = Field(min_length=1)
    content: str
    is_error: bool = False


type ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")
]


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: ToolID = Field(default_factory=uuid.uuid4)
    name: str = Field(pattern=r"^[a-z0-9_]+$", min_length=1)
    description: str
    parameters_schema: dict[str, Any]


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: AgentID = Field(default_factory=uuid.uuid4)
    name: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=128)
    system_prompt: str
    tool_ids: list[ToolID] = Field(default_factory=list)
    temperature: float = Field(0.7, ge=0.0, le=2.0, allow_inf_nan=False)
