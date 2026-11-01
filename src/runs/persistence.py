"""Short, synchronous transactions behind the executor's async boundary."""

import copy
import uuid
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from src.runs.exceptions import CancellationRequested, ExecutionLeaseLost, RunStateConflict
from src.runs.models import RunEvent, WorkflowRun
from src.runs.schemas import EventType, RunStatus

_TRANSITIONS = {
    RunStatus.PENDING.value: {
        RunStatus.PENDING.value,
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.RUNNING.value: {
        RunStatus.RUNNING.value,
        RunStatus.WAITING_HUMAN.value,
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.WAITING_HUMAN.value: {
        RunStatus.WAITING_HUMAN.value,
        RunStatus.RUNNING.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.COMPLETED.value: {RunStatus.COMPLETED.value},
    RunStatus.FAILED.value: {RunStatus.FAILED.value},
    RunStatus.CANCELLED.value: {RunStatus.CANCELLED.value},
}


def _assert_lease(run: WorkflowRun, token: uuid.UUID | None) -> None:
    if token is None:
        if run.execution_token is not None:
            raise ExecutionLeaseLost("An active run requires its execution token")
        return
    if (
        run.execution_token != token
        or run.lease_expires_at is None
        or run.lease_expires_at <= timezone.now()
    ):
        raise ExecutionLeaseLost("The run execution lease has expired or changed")


def _append_locked(
    run: WorkflowRun,
    event_type: str,
    payload: dict[str, Any],
    context_snapshot: dict[str, Any] | None,
) -> RunEvent:
    # Every writer locks the parent row, including the first event where no
    # child rows yet exist. The unique constraint remains the final guard.
    latest = RunEvent.objects.filter(run=run).aggregate(latest=Max("sequence"))["latest"] or 0
    return RunEvent.objects.create(
        run=run,
        sequence=latest + 1,
        event_type=event_type,
        payload=copy.deepcopy(payload),
        context_snapshot=copy.deepcopy(context_snapshot),
    )


@sync_to_async(thread_sensitive=True)
def load_run(run_id: uuid.UUID) -> WorkflowRun:
    return WorkflowRun.objects.select_related("workflow").get(id=run_id)


@sync_to_async(thread_sensitive=True)
def append_event(
    run_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    context_snapshot: dict[str, Any] | None = None,
    *,
    execution_token: uuid.UUID | None = None,
) -> RunEvent:
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(id=run_id)
        _assert_lease(run, execution_token)
        return _append_locked(run, event_type, payload, context_snapshot)


@sync_to_async(thread_sensitive=True)
def save_progress(
    run_id: uuid.UUID,
    *,
    status: str,
    checkpoint: dict[str, Any],
    output_data: dict[str, Any] | None = None,
    execution_token: uuid.UUID | None = None,
) -> None:
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(id=run_id)
        _assert_lease(run, execution_token)
        if status not in _TRANSITIONS.get(run.status, set()):
            raise RunStateConflict(f"Cannot transition run from {run.status} to {status}")
        if run.cancel_requested and status != RunStatus.CANCELLED.value:
            raise CancellationRequested("The run owner requested cancellation")
        previous = run.status
        run.status = status
        run.checkpoint = copy.deepcopy(checkpoint)
        fields = ["status", "checkpoint", "updated_at"]
        if status == RunStatus.COMPLETED.value:
            run.output_data = copy.deepcopy(output_data)
            fields.append("output_data")
        run.save(update_fields=fields)
        if status != previous:
            payload: dict[str, Any] = {"status": status}
            if status == RunStatus.FAILED.value and checkpoint.get("error"):
                payload["error"] = checkpoint["error"]
            _append_locked(run, EventType.STATUS_CHANGED.value, payload, None)
