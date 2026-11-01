"""Authenticated commands and bounded polling for durable workflow runs."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from src.api.auth import require_user
from src.runs.models import RunEvent, WorkflowRun
from src.runs.schemas import ResumeRequest, RunRequest

fastapi_router = APIRouter(tags=["Workflow Execution"])
Identity = Annotated[Any, Depends(require_user)]
IdempotencyKey = Annotated[str | None, Header(min_length=1, max_length=128)]


class AcceptedRun(BaseModel):
    run_id: uuid.UUID
    status: str


class RunDetail(BaseModel):
    run_id: uuid.UUID
    workflow_id: uuid.UUID
    status: str
    input_data: dict[str, Any]
    output_data: Any
    created_at: datetime
    updated_at: datetime


class EventDetail(BaseModel):
    id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    context_snapshot: dict[str, Any] | None
    timestamp: datetime


class EventPage(BaseModel):
    run_id: uuid.UUID
    status: str
    events: list[EventDetail]
    next_after: int
    has_more: bool


def command_error(exc):
    if isinstance(exc, ObjectDoesNotExist):
        return HTTPException(status_code=404, detail="Workflow or run not found")
    if isinstance(exc, (ValidationError, PydanticValidationError)):
        return HTTPException(status_code=422, detail="Invalid workflow or command data")
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="Conflicting workflow command")
    return HTTPException(status_code=400, detail=str(exc))


async def visible_run(run_id: uuid.UUID, user: Any) -> WorkflowRun:
    queryset = WorkflowRun.objects.select_related("workflow")
    if not user.is_staff:
        queryset = queryset.filter(workflow__owner_id=user.pk)
    try:
        return await queryset.aget(pk=run_id)
    except WorkflowRun.DoesNotExist as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@fastapi_router.post("/runs/execute", status_code=202, response_model=AcceptedRun)
async def execute_workflow(
    body: RunRequest,
    user: Identity,
    idempotency_key: IdempotencyKey = None,
):
    from src.runs.services import create_run

    try:
        run = await create_run(
            body.workflow_id, body.input_data, user, idempotency_key=idempotency_key
        )
    except (
        ObjectDoesNotExist,
        ValueError,
        ValidationError,
        PydanticValidationError,
        IntegrityError,
    ) as exc:
        raise command_error(exc) from exc
    return AcceptedRun(run_id=run.pk, status=run.status)


@fastapi_router.post("/runs/{run_id}/resume", status_code=202, response_model=AcceptedRun)
async def resume_workflow(
    run_id: uuid.UUID,
    body: ResumeRequest,
    user: Identity,
    idempotency_key: IdempotencyKey = None,
):
    from src.runs.services import enqueue_resume

    try:
        run = await enqueue_resume(
            run_id, body.model_dump(exclude_none=True), user, idempotency_key=idempotency_key
        )
    except (
        ObjectDoesNotExist,
        ValueError,
        ValidationError,
        PydanticValidationError,
        IntegrityError,
    ) as exc:
        raise command_error(exc) from exc
    return AcceptedRun(run_id=run.pk, status="queued")


@fastapi_router.post("/runs/{run_id}/cancel", status_code=202, response_model=AcceptedRun)
async def cancel_run(run_id: uuid.UUID, user: Identity):
    from src.runs.services import request_cancel

    try:
        run = await request_cancel(run_id, user)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise command_error(exc) from exc
    return AcceptedRun(run_id=run.pk, status=run.status)


@fastapi_router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, user: Identity):
    run = await visible_run(run_id, user)
    return RunDetail(
        run_id=run.pk,
        workflow_id=run.workflow.pk,
        status=run.status,
        input_data=run.input_data,
        output_data=run.output_data,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@fastapi_router.get("/runs/{run_id}/events", response_model=EventPage)
async def get_run_events(
    run_id: uuid.UUID,
    user: Identity,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    run = await visible_run(run_id, user)
    rows = [
        row
        async for row in RunEvent.objects.filter(run_id=run.pk, sequence__gt=after).order_by(
            "sequence"
        )[: limit + 1]
    ]
    page = rows[:limit]
    return EventPage(
        run_id=run.pk,
        status=run.status,
        events=[
            EventDetail(
                id=str(row.pk),
                sequence=row.sequence,
                type=row.event_type,
                payload=row.payload,
                context_snapshot=row.context_snapshot,
                timestamp=row.created_at,
            )
            for row in page
        ],
        next_after=page[-1].sequence if page else after,
        has_more=len(rows) > limit,
    )


class OperationsMetrics(BaseModel):
    collected_at: datetime
    run_counts: dict[str, int]
    command_counts: dict[str, int]
    oldest_pending_age_seconds: float = Field(ge=0)
    expired_processing_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


@fastapi_router.get("/operations", response_model=OperationsMetrics)
async def get_operations(user: Identity):
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="Staff access required")
    from src.api.operations import collect_operations

    return OperationsMetrics(**await collect_operations())
