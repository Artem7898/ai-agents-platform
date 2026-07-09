import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, UUID4

type WorkflowID = UUID4
type RunID = UUID4

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


class RunRequest(BaseModel):
    """DTO для запроса на запуск воркфлоу."""
    workflow_id: uuid.UUID
    input_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Входные данные для стартового узла DAG (например, {'prompt': '...'})"
    )


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class OutboxMessageSpec(BaseModel):
    """
    Схема для системной таблицы Outbox.
    Extra="forbid" защищает от случайного мусора в payload.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID4 = Field(default_factory=uuid.uuid4)
    topic: str = Field(..., description="Тип команды, например 'run_workflow'")
    payload: dict[str, Any] = Field(..., description="JSON payload с входными данными")
    status: OutboxStatus = OutboxStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    locked_at: datetime | None = None



class NodeKind:
    pass

class EventType:
    HITL_WAITING = "hitl_waiting"
