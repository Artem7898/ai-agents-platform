import importlib

import pytest
from asgiref.sync import sync_to_async
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection

from src.runs.models import RunEvent, WorkflowRun

pytestmark = pytest.mark.django_db(transaction=True)


async def test_legacy_events_and_definition_are_preserved(workflow_factory):
    workflow = await workflow_factory()
    run = await WorkflowRun.objects.acreate(
        workflow=workflow,
        input_data={"prompt": "preserve"},
        events=[
            {
                "type": "node_exit",
                "payload": {"node_id": "old"},
                "timestamp": "2026-07-14T10:00:00",
                "context_snapshot": {"result": "old"},
            }
        ],
    )
    migration = importlib.import_module("src.runs.migrations.0004_freeze_existing_runs")

    @sync_to_async(thread_sensitive=True)
    def migrate():
        migration.freeze_existing_runs(apps, connection.schema_editor())

    await migrate()
    await migrate()  # Re-running data import cannot duplicate existing events.
    await run.arefresh_from_db()
    assert run.input_data == {"prompt": "preserve"}
    assert run.workflow_snapshot["id"] == str(workflow.pk)
    assert run.workflow_snapshot["nodes"] == workflow.nodes
    assert await RunEvent.objects.filter(run=run).acount() == 1
    event = await RunEvent.objects.aget(run=run)
    assert event.sequence == 1
    assert event.context_snapshot == {"result": "old"}
    assert event.created_at.isoformat() == "2026-07-14T10:00:00+00:00"


async def test_billing_owner_backfill_preserves_frozen_and_unowned_runs(workflow_factory):
    original = await get_user_model().objects.acreate(username="original-billing-owner")
    current = await get_user_model().objects.acreate(username="current-workflow-owner")
    workflow = await workflow_factory(owner=current)
    pending = await WorkflowRun.objects.acreate(workflow=workflow)
    frozen = await WorkflowRun.objects.acreate(workflow=workflow, billing_owner=original)
    unowned_workflow = await workflow_factory()
    unowned = await WorkflowRun.objects.acreate(workflow=unowned_workflow)
    migration = importlib.import_module("src.runs.migrations.0006_workflowrun_billing_owner")

    @sync_to_async(thread_sensitive=True)
    def migrate():
        migration.freeze_billing_owners(apps, connection.schema_editor())

    await migrate()
    await migrate()
    await pending.arefresh_from_db()
    await frozen.arefresh_from_db()
    await unowned.arefresh_from_db()
    assert pending.billing_owner_id == current.pk
    assert frozen.billing_owner_id == original.pk
    assert unowned.billing_owner_id is None
