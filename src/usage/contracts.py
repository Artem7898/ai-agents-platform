"""Transport-facing values; these contain no provider credentials or content."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class InvocationContext:
    run_id: UUID
    node_id: str
    logical_call: int
    execution_token: UUID | None = None


@dataclass(frozen=True)
class Reservation:
    id: UUID
    reserved_tokens: int
    attempt: int


class QuotaExceeded(ValueError):
    def __init__(self, invocation_id: UUID, *, scope: str = "owner"):
        self.invocation_id = invocation_id
        message = (
            "Run token budget exceeded"
            if scope == "run"
            else "The owner's LLM quota has been exhausted"
        )
        super().__init__(message)
