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

    


class NodeKind:
    pass


class EventType:
    pass