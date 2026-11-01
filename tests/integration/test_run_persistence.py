"""Real ORM checks for atomic status/event writes and stale-worker fencing."""

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.db import connections
from django.utils import timezone

from src.runs import persistence
from src.runs.exceptions import CancellationRequested, ExecutionLeaseLost, RunStateConflict
from src.runs.models import RunEvent, WorkflowRun
from src.workflows.models import Workflow
from src.workflows.schemas import WorkflowSpec

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest_asyncio.fixture(autouse=True)
async def close_async_connections():
    yield
    await sync_to_async(connections.close_all, thread_sensitive=True)()


@pytest_asyncio.fixture
async def run():
    spec = WorkflowSpec(
        name=f"persistence-{uuid.uuid4()}", nodes=[{"id": "approve", "kind": "hitl_approval"}]
    )
    workflow = Workflow.from_spec(spec)
    await workflow.asave()
    return await WorkflowRun.objects.acreate(
        workflow=workflow, workflow_snapshot=spec.model_dump(mode="json")
    )


async def test_status_and_event_are_committed_together(run):
    await persistence.save_progress(run.id, status="running", checkpoint={"completed": []})
    await run.arefresh_from_db()
    event = await RunEvent.objects.aget(run=run)
    assert run.status == "running"
    assert run.checkpoint == {"completed": []}
    assert event.event_type == "status_changed"
    assert event.payload == {"status": "running"}


async def test_failed_terminal_state_cannot_be_overwritten(run):
    await persistence.save_progress(run.id, status="failed", checkpoint={"error": "node failed"})
    with pytest.raises(RunStateConflict):
        await persistence.save_progress(run.id, status="completed", checkpoint={})
    await run.arefresh_from_db()
    assert run.status == "failed"
    event = await RunEvent.objects.aget(run=run)
    assert event.payload["error"] == "node failed"


async def test_event_failure_rolls_back_status_and_checkpoint(run, monkeypatch):
    def broken_append(*_args, **_kwargs):
        raise ValueError("simulated event insert error")

    monkeypatch.setattr(persistence, "_append_locked", broken_append)
    with pytest.raises(ValueError, match="event insert"):
        await persistence.save_progress(
            run.id, status="running", checkpoint={"completed": ["approve"]}
        )
    await run.arefresh_from_db()
    assert run.status == "pending"
    assert run.checkpoint == {}


async def test_expired_and_wrong_execution_tokens_cannot_write(run):
    token = uuid.uuid4()
    await WorkflowRun.objects.filter(id=run.id).aupdate(
        execution_token=token, lease_expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(ExecutionLeaseLost):
        await persistence.append_event(run.id, "node_enter", {})
    with pytest.raises(ExecutionLeaseLost):
        await persistence.save_progress(
            run.id, status="running", checkpoint={}, execution_token=uuid.uuid4()
        )
    await persistence.save_progress(run.id, status="running", checkpoint={}, execution_token=token)
    await WorkflowRun.objects.filter(id=run.id).aupdate(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(ExecutionLeaseLost):
        await persistence.append_event(run.id, "node_enter", {}, execution_token=token)
    assert await RunEvent.objects.filter(run=run).acount() == 1


async def test_cancel_request_blocks_completion_race(run):
    await persistence.save_progress(run.id, status="running", checkpoint={})
    await WorkflowRun.objects.filter(id=run.id).aupdate(cancel_requested=True)
    with pytest.raises(CancellationRequested):
        await persistence.save_progress(
            run.id, status="completed", checkpoint={}, output_data={"result": "late"}
        )
    await persistence.save_progress(run.id, status="cancelled", checkpoint={})
    await run.arefresh_from_db()
    assert run.status == "cancelled"
    assert run.output_data is None


async def test_concurrent_appends_keep_unique_monotonic_sequences(run):
    await asyncio.gather(
        *(persistence.append_event(run.id, "node_exit", {"producer": i}) for i in range(12))
    )
    records = [record async for record in RunEvent.objects.filter(run=run).order_by("sequence")]
    assert [event.sequence for event in records] == list(range(1, 13))
    assert {event.payload["producer"] for event in records} == set(range(12))


async def test_real_executor_checkpoint_and_result_use_immutable_graph(run):
    from src.runs.executor import WorkflowExecutor

    spec = WorkflowSpec(
        id=run.workflow_id,
        name="immutable-condition",
        nodes=[
            {
                "id": "decide",
                "kind": "condition",
                "config": {"condition": {"path": "input.answer", "op": "eq", "value": 42}},
            }
        ],
    )
    await WorkflowRun.objects.filter(id=run.id).aupdate(
        workflow_snapshot=spec.model_dump(mode="json"), input_data={"answer": 42}
    )
    # The mutable definition differs from the definition captured for this run.
    await Workflow.objects.filter(id=run.workflow_id).aupdate(nodes=[])
    await WorkflowExecutor(run.id).execute()
    await run.arefresh_from_db()
    assert run.status == "completed"
    assert run.output_data == {"result": True, "results": {"decide": True}}
    assert run.checkpoint["completed"] == ["decide"]
    statuses = [
        event.payload["status"]
        async for event in RunEvent.objects.filter(run=run, event_type="status_changed")
    ]
    assert statuses == ["running", "completed"]


async def test_real_executor_validation_error_stays_failed(run):
    from src.runs.executor import WorkflowExecutor

    spec = WorkflowSpec(
        id=run.workflow_id,
        name="incompatible-comparison",
        nodes=[
            {
                "id": "decide",
                "kind": "condition",
                "config": {"condition": {"path": "input.answer", "op": "gt", "value": 42}},
            }
        ],
    )
    await WorkflowRun.objects.filter(id=run.id).aupdate(
        workflow_snapshot=spec.model_dump(mode="json"), input_data={"answer": "text"}
    )
    await WorkflowExecutor(run.id).execute()
    await run.arefresh_from_db()
    assert run.status == "failed"
    statuses = [
        event.payload["status"]
        async for event in RunEvent.objects.filter(run=run, event_type="status_changed")
    ]
    assert statuses == ["running", "failed"]
