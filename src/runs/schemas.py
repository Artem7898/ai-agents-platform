"""Run commands, states and checkpoint contracts."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import UUID4, BaseModel, ConfigDict, Field

type WorkflowID = UUID4
type RunID = UUID4


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    STATUS_CHANGED = "status_changed"
    NODE_ENTER = "node_enter"
    NODE_EXIT = "node_exit"
    NODE_SKIPPED = "node_skipped"
    NODE_FAILED = "node_failed"
    HITL_WAITING = "hitl_waiting"
    HITL_RESUMED = "hitl_resumed"
    TOOL_CALL = "tool_call"
    USAGE = "usage"


class WorkflowRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: RunID = Field(default_factory=uuid.uuid4)
    workflow_id: WorkflowID
    status: RunStatus = RunStatus.PENDING
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: uuid.UUID
    input_data: dict[str, Any] = Field(default_factory=dict)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class OutboxMessageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    topic: str
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    locked_at: datetime | None = None


class RunCheckpoint(BaseModel):
    """Private persisted scheduler state, separate from user input."""

    model_config = ConfigDict(extra="forbid")
    context: dict[str, Any] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    waiting: list[str] = Field(default_factory=list)
    human_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    node_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
    executed_steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    reserved_tool_calls: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=lambda: {"total_tokens": 0})
    elapsed_seconds: float = Field(default=0, ge=0)
    started_at: str | None = None
    error: str | None = None
