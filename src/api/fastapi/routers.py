"""
FastAPI Controllers.
Слой маршрутизации для High-load и Streaming эндпоинтов.
"""
import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.workflows.models import Workflow
from src.runs.models import WorkflowRun
from src.runs.schemas import RunRequest, RunStatus
from src.runs.executor import WorkflowExecutor

import structlog

log = structlog.get_logger()

# Маршрутизатор с префиксом (монтируется в config/asgi.py)
fastapi_router = APIRouter(prefix="/api/v2", tags=["execution"])


# ============================================================================
# RESPONSE SCHEMAS (DTOs)
# ============================================================================

class RunResponse(BaseModel):
    """DTO для мгновенного ответа при старте."""
    run_id: uuid.UUID
    workflow_id: uuid.UUID
    status: str


class RunStatusResponse(BaseModel):
    """
    DTO для чтения состояния фонового выполнения (Polling).
    Содержит чистые бизнес-данные, скрытая инфраструктура отсечена.
    """
    run_id: uuid.UUID
    workflow_id: uuid.UUID
    status: str  # Текущий статус (PENDING, RUNNING, COMPLETED, FAILED)
    input_data: dict[str, Any]
    output_data: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []  # Распарсенный JSON массив событий (Time-Travel)


# ============================================================================
# CONTROLLERS
# ============================================================================

@fastapi_router.post("/runs/execute", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def execute_workflow_run(request: RunRequest):
    """Запускает выполнение Workflow в фоновом режиме."""
    try:
        workflow = await Workflow.objects.aget(id=request.workflow_id)
    except Workflow.DoesNotExist:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {request.workflow_id} not found"
        )

    run = await WorkflowRun.objects.acreate(
        workflow=workflow,
        status=RunStatus.PENDING.value,
        input_data=request.input_data
    )

    # Запускаем в фоне
    asyncio.create_task(_run_workflow_background_task(run.id))

    log.info("workflow_run_accepted", run_id=str(run.id), workflow_id=str(workflow.id))
    return RunResponse(
        run_id=run.id,
        workflow_id=workflow.id,
        status=RunStatus.PENDING.value
    )


@fastapi_router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: uuid.UUID):
    """
    Возвращает текущий статус и результаты фонового выполнения.

    Используется клиентом для Polling (опроса состояния).
    Возвращает события из JSONB поля для отрисовки Time-Travel отладки в UI.
    """
    try:
        # Используем aget для асинхронного запроса к БД
        run = await WorkflowRun.objects.aget(id=run_id)

        return RunStatusResponse(
            run_id=run.id,
            workflow_id=run.workflow_id,
            status=run.status,
            input_data=run.input_data,
            output_data=run.output_data,
            # Если events хранятся как JSON-строка, конвертируем в список объектов
            events=run.events if isinstance(run.events, list) else []
        )

    except WorkflowRun.DoesNotExist:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WorkflowRun {run_id} not found"
        )


# ============================================================================
# BACKGROUND WORKER
# ============================================================================

async def _run_workflow_background_task(run_id: uuid.UUID):
    """Фоновая задача выполнения DAG."""
    try:
        executor = WorkflowExecutor(run_id)
        await executor.execute()
    except Exception as e:
        log.exception("background_task_failed", run_id=str(run_id), error=str(e))
        try:
            run = await WorkflowRun.objects.aget(id=run_id)
            run.status = RunStatus.FAILED.value
            run.events.append({
                "id": str(uuid.uuid4()),
                "type": "system_error",
                "payload": {"error": str(e)},
                "timestamp": datetime.utcnow().isoformat()
            })
            await run.asave(update_fields=["status", "events"])
        except Exception:
            log.critical("failed_to_update_run_status_on_error", run_id=str(run_id))