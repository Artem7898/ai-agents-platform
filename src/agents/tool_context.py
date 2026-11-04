"""Explicit workflow identities available to trusted tool implementations.

The model cannot supply this dependency. It carries no ORM objects, provider
credentials or general backend context.
"""

from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from pydantic import PydanticSchemaGenerationError


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    run_id: UUID
    workflow_id: UUID
    node_id: str
    tool_call_id: str
    owner_id: int | None
    idempotency_key: str

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> NoReturn:
        # Exact dependency parameters are removed before argument schemas are
        # built. Refuse every deserialization route for a wrapped dependency.
        raise PydanticSchemaGenerationError("Workflow context cannot be a model argument")

    def __post_init__(self):
        if not isinstance(self.run_id, UUID) or not isinstance(self.workflow_id, UUID):
            raise ValueError("Tool context requires workflow and run UUIDs")
        for value, maximum in (
            (self.node_id, 128),
            (self.tool_call_id, 128),
            (self.idempotency_key, 512),
        ):
            if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                raise ValueError("Tool context requires bounded identifiers")
        if self.owner_id is not None and (type(self.owner_id) is not int or self.owner_id < 1):
            raise ValueError("Tool context requires a user identity or None")
