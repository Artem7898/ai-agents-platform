"""Versioned, bounded contracts for model recommendations and human decisions."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from src.workflows.output_schemas import OutputSchemaRegistry

type TicketCategory = Literal["Facturación", "Acceso", "Soporte técnico", "Otros"]

CATEGORIES = ("Facturación", "Acceso", "Soporte técnico", "Otros")


class StrictContract(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


@OutputSchemaRegistry.register(
    "ticket_classification",
    1,
    description="A ticket category and brief reason, subject to explicit human confirmation.",
)
class TicketClassificationV1(StrictContract):
    category: TicketCategory
    reason: str = Field(min_length=1, max_length=1000)


class ConfirmedCategoryV1(StrictContract):
    approved: StrictBool
    category: TicketCategory
    note: str = Field(default="", max_length=1000)

    @field_validator("approved")
    @classmethod
    def require_explicit_approval(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Saving requires explicit human approval")
        return value


class SinkSubmissionV1(StrictContract):
    """Only the trusted adapter supplies run identities and the destination key."""

    run_id: UUID
    workflow_id: UUID
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=1, max_length=512)
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    classification: TicketClassificationV1
    confirmation: ConfirmedCategoryV1

    @model_validator(mode="after")
    def require_stable_key(self):
        if self.idempotency_key != f"{self.run_id}:{self.node_id}:tool":
            raise ValueError("The destination key must identify the explicit save node")
        return self
