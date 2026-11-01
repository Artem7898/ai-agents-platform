"""Durable command acceptance, ownership and idempotency at the database boundary."""

import uuid

import pytest
from django.core.exceptions import ObjectDoesNotExist

from src.agents.models import Agent
from src.agents.schemas import AgentStatus
from src.runs.models import OutboxMessage, RunEvent, WorkflowRun
from src.runs.schemas import RunStatus
from src.runs.services import create_run, enqueue_resume, request_cancel
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def make_workflow(user, **overrides):
    values = {
        "owner": user,
        "name": f"Command workflow {uuid.uuid4().hex}",
        "nodes": [
            {
                "id": "check",
                "kind": "condition",
                "config": {
                    "condition": {"path": "input.approved", "op": "truthy"},
                },
            }
        ],
        "edges": [],
    }
    values.update(overrides)
    return await Workflow.objects.acreate(**values)


async def make_user(django_user_model, username="commands-owner", **extra):
    return await django_user_model.objects.acreate(username=username, **extra)


async def test_create_atomically_persists_snapshot_and_command(django_user_model):
    user = await make_user(django_user_model)
    workflow = await make_workflow(user)
    run = await create_run(workflow.pk, {"approved": True}, user)
    command = await OutboxMessage.objects.aget(run=run)
    assert run.status == RunStatus.PENDING
    assert run.input_data == {"approved": True}
    assert run.workflow_snapshot["nodes"] == workflow.to_spec().model_dump(mode="json")["nodes"]
    assert "max_duration_seconds" in run.workflow_snapshot["limits"]
    assert command.topic == "run_workflow"
    assert command.payload == {"run_id": str(run.pk)}
    assert await RunEvent.objects.filter(run=run, sequence=1).aexists()


async def test_create_key_reuses_run_and_rejects_changed_body(django_user_model):
    user = await make_user(django_user_model)
    workflow = await make_workflow(user)
    first = await create_run(workflow.pk, {"approved": True}, user, idempotency_key="same")
    retry = await create_run(workflow.pk, {"approved": True}, user, idempotency_key="same")
    assert retry.pk == first.pk
    assert await WorkflowRun.objects.acount() == 1
    assert await OutboxMessage.objects.acount() == 1
    with pytest.raises(ValueError, match="different command data"):
        await create_run(workflow.pk, {"approved": False}, user, idempotency_key="same")
    assert await WorkflowRun.objects.acount() == 1


async def test_create_key_is_scoped_to_user(django_user_model):
    owner = await make_user(django_user_model)
    other = await make_user(django_user_model, "other-commands-owner")
    first = await create_run((await make_workflow(owner)).pk, {}, owner, idempotency_key="key")
    second = await create_run((await make_workflow(other)).pk, {}, other, idempotency_key="key")
    assert first.pk != second.pk


async def test_create_denies_foreign_workflow(django_user_model):
    owner = await make_user(django_user_model)
    other = await make_user(django_user_model, "other-commands-owner")
    workflow = await make_workflow(owner)
    with pytest.raises(ObjectDoesNotExist):
        await create_run(workflow.pk, {}, other)
    assert await WorkflowRun.objects.acount() == 0
    assert await OutboxMessage.objects.acount() == 0


async def test_agent_snapshot_is_frozen_and_owner_checked(django_user_model):
    user = await make_user(django_user_model)
    agent = await Agent.objects.acreate(
        owner=user,
        name="Frozen",
        model_name="test-model",
        system_prompt="original",
        status=AgentStatus.ACTIVE,
        tool_ids=[],
    )
    workflow = await make_workflow(
        user,
        nodes=[
            {
                "id": "answer",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
            }
        ],
    )
    run = await create_run(workflow.pk, {}, user)
    await Agent.objects.filter(pk=agent.pk).aupdate(system_prompt="modified")
    assert run.agent_snapshots[str(agent.pk)]["spec"]["system_prompt"] == "original"
    other = await make_user(django_user_model, "other-commands-owner")
    await Agent.objects.filter(pk=agent.pk).aupdate(owner=other)
    with pytest.raises(ValueError, match="same owner"):
        await create_run(workflow.pk, {}, user)
    assert await WorkflowRun.objects.acount() == 1


async def test_inactive_agent_cannot_be_submitted(django_user_model):
    user = await make_user(django_user_model)
    agent = await Agent.objects.acreate(
        owner=user,
        name="Draft",
        model_name="test-model",
        system_prompt="test",
        tool_ids=[],
    )
    workflow = await make_workflow(
        user,
        nodes=[
            {
                "id": "answer",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
            }
        ],
    )
    with pytest.raises(ValueError, match="must be active"):
        await create_run(workflow.pk, {}, user)
    assert await OutboxMessage.objects.acount() == 0


async def waiting_run(user, **checkpoint_overrides):
    workflow = await make_workflow(user)
    return await WorkflowRun.objects.acreate(
        workflow=workflow,
        status=RunStatus.WAITING_HUMAN,
        workflow_snapshot=workflow.to_spec().model_dump(mode="json"),
        checkpoint={"waiting": ["approval"], **checkpoint_overrides},
    )


async def test_resume_is_durable_and_duplicate_safe(django_user_model):
    user = await make_user(django_user_model)
    run = await waiting_run(user)
    await enqueue_resume(run.pk, {"input": {"approved": True}}, user)
    await enqueue_resume(run.pk, {"input": {"approved": True}}, user)
    command = await OutboxMessage.objects.aget(run=run)
    assert command.topic == "resume_workflow"
    assert command.payload["human_input"] == {"node_id": "approval", "input": {"approved": True}}
    assert command.payload["checkpoint_hash"]
    await run.arefresh_from_db()
    assert run.status == RunStatus.WAITING_HUMAN
    with pytest.raises(ValueError, match="different resume"):
        await enqueue_resume(run.pk, {"input": {"approved": False}}, user)
    assert await OutboxMessage.objects.acount() == 1


async def test_resume_key_retry_works_after_run_advances(django_user_model):
    user = await make_user(django_user_model)
    run = await waiting_run(user)
    await enqueue_resume(run.pk, {"input": {}}, user, idempotency_key="approval")
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(status=RunStatus.COMPLETED)
    retry = await enqueue_resume(run.pk, {"input": {}}, user, idempotency_key="approval")
    assert retry.pk == run.pk
    assert await OutboxMessage.objects.acount() == 1
    with pytest.raises(ValueError, match="different command data"):
        await enqueue_resume(run.pk, {"input": {"x": 1}}, user, idempotency_key="approval")


async def test_multiple_waiting_nodes_require_explicit_selection(django_user_model):
    user = await make_user(django_user_model)
    run = await waiting_run(user, waiting=["approval", "second"])
    with pytest.raises(ValueError, match="node_id is required"):
        await enqueue_resume(run.pk, {"input": {}}, user)
    with pytest.raises(ValueError, match="not waiting"):
        await enqueue_resume(run.pk, {"node_id": "missing", "input": {}}, user)
    await enqueue_resume(run.pk, {"node_id": "second", "input": {}}, user)
    assert await OutboxMessage.objects.acount() == 1


async def test_foreign_resume_and_cancel_are_hidden(django_user_model):
    owner = await make_user(django_user_model)
    other = await make_user(django_user_model, "other-commands-owner")
    run = await waiting_run(owner)
    with pytest.raises(ObjectDoesNotExist):
        await enqueue_resume(run.pk, {"input": {}}, other)
    with pytest.raises(ObjectDoesNotExist):
        await request_cancel(run.pk, other)
    assert await OutboxMessage.objects.acount() == 0


@pytest.mark.parametrize("initial", [RunStatus.PENDING, RunStatus.WAITING_HUMAN])
async def test_cancel_unstarted_run_is_terminal_and_idempotent(django_user_model, initial):
    user = await make_user(django_user_model)
    workflow = await make_workflow(user)
    run = await WorkflowRun.objects.acreate(workflow=workflow, status=initial)
    cancelled = await request_cancel(run.pk, user)
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.cancel_requested is True
    await request_cancel(run.pk, user)
    assert await RunEvent.objects.filter(run=run).acount() == 1


async def test_running_cancel_only_requests_stop(django_user_model):
    user = await make_user(django_user_model)
    workflow = await make_workflow(user)
    run = await WorkflowRun.objects.acreate(workflow=workflow, status=RunStatus.RUNNING)
    cancelled = await request_cancel(run.pk, user)
    assert cancelled.status == RunStatus.RUNNING
    assert cancelled.cancel_requested is True


async def test_independent_approvals_can_be_queued_together(django_user_model):
    user = await make_user(django_user_model)
    run = await waiting_run(user, waiting=["first", "second"])
    await enqueue_resume(run.pk, {"node_id": "first", "input": {"approved": True}}, user)
    await enqueue_resume(run.pk, {"node_id": "second", "input": {"approved": False}}, user)
    assert await OutboxMessage.objects.filter(run=run).acount() == 2
    # An unrelated checkpoint update cannot bypass a pending decision conflict.
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        checkpoint={"waiting": ["first", "second"], "elapsed_seconds": 1}
    )
    await enqueue_resume(run.pk, {"node_id": "first", "input": {"approved": True}}, user)
    with pytest.raises(ValueError, match="different resume"):
        await enqueue_resume(run.pk, {"node_id": "first", "input": {"approved": False}}, user)
    assert await OutboxMessage.objects.filter(run=run).acount() == 2


@pytest.mark.postgresql
async def test_concurrent_cross_run_resume_key_conflict_is_controlled(
    django_user_model, monkeypatch
):
    import asyncio
    from threading import Barrier

    from django.db import close_old_connections, connection, connections

    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL transactions")
    user = await make_user(django_user_model)
    first = await waiting_run(user)
    second = await waiting_run(user)
    barrier = Barrier(2)
    original_create = OutboxMessage.objects.create

    def synchronized_create(**kwargs):
        if kwargs.get("topic") == "resume_workflow":
            barrier.wait(timeout=10)
        return original_create(**kwargs)

    monkeypatch.setattr(OutboxMessage.objects, "create", synchronized_create)

    def submit(run_id):
        close_old_connections()
        try:
            # Independent sync transactions reproduce concurrent ASGI contexts.
            result = enqueue_resume.func(run_id, {"input": {}}, user, idempotency_key="race")
            return str(result.pk)
        except ValueError as error:
            return str(error)
        finally:
            connections.close_all()

    results = await asyncio.gather(
        asyncio.to_thread(submit, first.pk), asyncio.to_thread(submit, second.pk)
    )
    assert sum("different command data" in result for result in results) == 1
    assert sum(result in {str(first.pk), str(second.pk)} for result in results) == 1
    assert await OutboxMessage.objects.filter(topic="resume_workflow").acount() == 1
