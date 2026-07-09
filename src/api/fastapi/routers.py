"""
FastAPI Routers (Execution & Streaming Layer).
"""
import uuid
from django.db import transaction
from fastapi import APIRouter, HTTPException

from src.runs.models import WorkflowRun
from src.runs.outbox import OutboxPublisher
from src.runs.executor import WorkflowExecutor
from django.core.exceptions import ObjectDoesNotExist

fastapi_router = APIRouter(tags=["Workflow Execution"])


@fastapi_router.post("/runs/execute", status_code=202)
async def execute_workflow(body: dict):
    workflow_id_str = body.get("workflow_id")
    if not workflow_id_str:
        raise HTTPException(status_code=400, detail="Missing 'workflow_id'")

    try:
        workflow_id = uuid.UUID(workflow_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format")

    async with transaction.atomic():
        run = WorkflowRun(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            status="pending",
            input_data=body.get("input_data", {}),
            events=[]
        )
        await run.asave()

        await OutboxPublisher.publish(
            topic="run_workflow",
            payload={"run_id": str(run.id)}
        )

    return {
        "run_id": str(run.id),
        "status": "pending"
    }


@fastapi_router.post("/runs/{run_id}/resume", status_code=202)
async def resume_workflow(run_id: uuid.UUID, body: dict):
    """
    Этап 3 ТЗ: Human-in-the-loop (HITL).
    Достает контекст из БД и перезапускает executor с нужного места графа.
    """
    human_input = body.get("input", {})

    executor = WorkflowExecutor(run_id=run_id)

    try:
        # В проде (Этап 2) это тоже должно идти через Outbox,
        # но для текущей архитектуры запускаем корутину напрямую
        await executor.resume(human_input)
        return {"run_id": str(run_id), "status": "resumed"}
    except ObjectDoesNotExist:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@fastapi_router.get("/runs/{run_id}/events")
async def get_run_events(run_id: uuid.UUID):
    """
    Этап 4 ТЗ: Time-Travel Debugging API.
    Отдает массив событий для отрисовки Timeline на фронтенде.
    Каждое событие содержит context_snapshot, позволяя увидеть
    состояние памяти графа на любом шаге без пересчета.
    """
    try:
        # Оптимизация БД: выбираем ТОЛЬКО нужные поля (не дергаем тяжелый workflow)
        run = await WorkflowRun.objects.values(
            "id", "status", "events", "created_at"
        ).aget(id=run_id)

        return {
            "run_id": str(run["id"]),
            "status": run["status"],
            "created_at": run["created_at"].isoformat(),
            "events": run["events"]  # Прямая отдача JSONB массива
        }

    except ObjectDoesNotExist:
        raise HTTPException(status_code=404, detail=f"WorkflowRun {run_id} not found")