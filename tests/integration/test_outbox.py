"""Real database checks for claim, acknowledgement, recovery and worker shutdown."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import override_settings
from django.utils import timezone

from src.runs.models import OutboxMessage, RunEvent, WorkflowRun
from src.runs.outbox import OutboxWorker, _claim, _finish, _heartbeat
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)


@pytest_asyncio.fixture
async def close_worker_connections():
    yield
    # pytest-django closes the test thread's connection; async ORM work uses
    # another thread whose connection must be closed before dropping the DB.
    await sync_to_async(connections.close_all, thread_sensitive=True)()


@pytest.fixture
def command():
    owner = get_user_model().objects.create_user(username=f"worker-{uuid.uuid4()}")
    workflow = Workflow.objects.create(
        owner=owner,
        name=f"Worker workflow {uuid.uuid4()}",
        nodes=[
            {
                "id": "start",
                "kind": "condition",
                "config": {"condition": {"path": "enabled", "op": "exists"}},
            }
        ],
        edges=[],
    )
    run = WorkflowRun.objects.create(
        workflow=workflow,
        workflow_snapshot=workflow.to_spec().model_dump(mode="json"),
        checkpoint={"context": {"kept": "after-crash"}},
    )
    message = OutboxMessage.objects.create(
        run=run, topic="run_workflow", payload={"run_id": str(run.id)}
    )
    return run, message


def expire(run, message):
    past = timezone.now() - timedelta(seconds=1)
    WorkflowRun.objects.filter(id=run.id).update(lease_expires_at=past)
    OutboxMessage.objects.filter(id=message.id).update(lease_expires_at=past)


def test_claim_sets_the_same_exclusive_token_and_expiry_on_run_and_message(command):
    run, message = command
    claimed = _claim(1)
    assert [item.id for item in claimed] == [message.id]
    run.refresh_from_db()
    message.refresh_from_db()
    assert message.status == "processing"
    assert message.attempts == 1
    assert message.execution_token is not None
    assert run.execution_token == message.execution_token
    assert run.lease_expires_at == message.lease_expires_at
    assert run.lease_expires_at > timezone.now()
    assert _claim(1) == []


def test_expired_claim_can_be_recovered_without_losing_checkpoint(command):
    run, message = command
    original = _claim(1)[0].execution_token
    expire(run, message)
    reclaimed = _claim(1)
    assert [item.id for item in reclaimed] == [message.id]
    run.refresh_from_db()
    message.refresh_from_db()
    assert message.execution_token != original
    assert message.attempts == 2
    assert run.execution_token == message.execution_token
    assert run.checkpoint == {"context": {"kept": "after-crash"}}


def test_old_worker_cannot_acknowledge_or_extend_successor_lease(command):
    run, message = command
    original = _claim(1)[0].execution_token
    expire(run, message)
    successor = _claim(1)[0].execution_token
    assert not _finish(message.id, original, None)
    assert not _heartbeat(message.id, original)
    run.refresh_from_db()
    message.refresh_from_db()
    assert message.status == "processing"
    assert message.execution_token == successor
    assert run.execution_token == successor


def test_expired_worker_cannot_acknowledge_before_a_successor_claims(command):
    run, message = command
    token = _claim(1)[0].execution_token
    expire(run, message)
    assert not _finish(message.id, token, None)
    message.refresh_from_db()
    assert message.status == "processing"
    assert message.execution_token == token


def test_heartbeat_cannot_revive_an_expired_run_lease(command):
    run, message = command
    token = _claim(1)[0].execution_token
    WorkflowRun.objects.filter(id=run.id).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert not _heartbeat(message.id, token)


def test_successful_acknowledgement_clears_only_its_own_lease(command):
    run, message = command
    token = _claim(1)[0].execution_token
    assert _finish(message.id, token, None)
    run.refresh_from_db()
    message.refresh_from_db()
    assert message.status == "completed"
    assert message.execution_token is None
    assert message.lease_expires_at is None
    assert run.execution_token is None
    assert run.lease_expires_at is None
    assert not _finish(message.id, token, None)


def test_failure_is_delayed_and_not_immediately_reclaimed(command):
    _, message = command
    token = _claim(1)[0].execution_token
    before = timezone.now()
    assert _finish(message.id, token, "Temporary provider outage")
    message.refresh_from_db()
    assert message.status == "pending"
    assert message.available_at > before
    assert message.last_error == "Temporary provider outage"
    assert _claim(1) == []
    OutboxMessage.objects.filter(id=message.id).update(available_at=before)
    assert _claim(1)[0].attempts == 2


@override_settings(OUTBOX_MAX_ATTEMPTS=2)
def test_retry_exhaustion_marks_the_run_failed_and_appends_one_event(command):
    run, message = command
    first = _claim(1)[0].execution_token
    assert _finish(message.id, first, "First failure")
    OutboxMessage.objects.filter(id=message.id).update(available_at=timezone.now())
    last = _claim(1)[0].execution_token
    assert _finish(message.id, last, "Second failure")
    run.refresh_from_db()
    message.refresh_from_db()
    assert message.status == "failed"
    assert message.attempts == 2
    assert run.status == "failed"
    assert run.execution_token is None
    assert "retry limit" in run.checkpoint["error"].lower()
    assert list(RunEvent.objects.filter(run=run).values_list("event_type", flat=True)) == [
        "status_changed"
    ]
    assert _claim(1) == []


@override_settings(OUTBOX_MAX_ATTEMPTS=1)
def test_crash_after_last_attempt_fails_run_when_expired_lease_is_revisited(command):
    run, message = command
    _claim(1)
    expire(run, message)
    assert _claim(1) == []
    run.refresh_from_db()
    message.refresh_from_db()
    assert run.status == "failed"
    assert message.status == "failed"
    assert run.event_records.count() == 1
    assert _claim(1) == []
    assert run.event_records.count() == 1


def test_a_terminal_run_is_not_executed_again(command):
    run, message = command
    WorkflowRun.objects.filter(id=run.id).update(status="completed")
    assert _claim(1) == []
    message.refresh_from_db()
    assert message.status == "completed"
    assert message.attempts == 0


@pytest.mark.postgresql
def test_independent_workers_cannot_claim_duplicate_commands_for_one_run(command):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row-lock semantics")
    run, first = command
    second = OutboxMessage.objects.create(
        run=run, topic="run_workflow", payload={"run_id": str(run.id)}
    )
    ready = Barrier(2)

    def claim_from_independent_connection():
        close_old_connections()
        try:
            ready.wait(timeout=5)
            return [message.id for message in _claim(1)]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_from_independent_connection) for _ in range(2)]
        claimed = [value for future in futures for value in future.result(timeout=10)]
    assert len(claimed) == 1
    assert claimed[0] in {first.id, second.id}
    assert OutboxMessage.objects.filter(run=run, status="processing").count() == 1
    assert OutboxMessage.objects.filter(run=run, status="pending").count() == 1
    assert _claim(1) == []


@override_settings(OUTBOX_LEASE_SECONDS=0.3)
async def test_heartbeat_keeps_a_long_running_command_alive(
    command, monkeypatch, close_worker_connections
):
    from src.runs import outbox

    run, message = command
    pulses = []
    real_heartbeat = outbox._heartbeat

    def heartbeat(*args):
        pulses.append(args)
        return real_heartbeat(*args)

    monkeypatch.setattr(outbox, "_heartbeat", heartbeat)

    async def handler(topic, payload, token):
        assert topic == "run_workflow"
        assert payload["run_id"] == str(run.id)
        assert token is not None
        await asyncio.sleep(0.45)
        await WorkflowRun.objects.filter(id=run.id, execution_token=token).aupdate(
            status="completed"
        )

    assert await OutboxWorker.process_next_batch(handler, batch_size=1) == 1
    await message.arefresh_from_db()
    await run.arefresh_from_db()
    assert len(pulses) >= 2
    assert message.status == "completed"
    assert run.execution_token is None


@pytest.mark.parametrize("heartbeat_raises", [False, True])
@override_settings(OUTBOX_LEASE_SECONDS=0.15)
async def test_lost_or_failed_heartbeat_cancels_work_without_acknowledging(
    command, monkeypatch, heartbeat_raises, close_worker_connections
):
    from src.runs import outbox

    _, message = command
    cancelled = asyncio.Event()
    finish = Mock()

    def heartbeat(*_args):
        if heartbeat_raises:
            raise RuntimeError("Heartbeat database connection failed")
        return False

    monkeypatch.setattr(outbox, "_heartbeat", heartbeat)
    # Synchronous spy should never be invoked by the async boundary.
    monkeypatch.setattr(outbox, "_finish", finish)

    async def handler(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    assert await asyncio.wait_for(OutboxWorker.process_next_batch(handler, 1), timeout=2) == 0
    assert cancelled.is_set()
    finish.assert_not_called()
    await message.arefresh_from_db()
    assert message.status == "processing"


async def test_worker_shutdown_cancels_child_and_leaves_command_recoverable(
    command, close_worker_connections
):
    _, message = command
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(OutboxWorker.process_next_batch(handler, 1))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    await message.arefresh_from_db()
    assert message.status == "processing"
    assert message.execution_token is not None


async def test_worker_handler_uses_run_id_token_and_routes_resume(
    monkeypatch, close_worker_connections
):
    from src.runs.management.commands import runworker

    executor = type("FakeExecutor", (), {"execute": AsyncMock(), "resume": AsyncMock()})()
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return executor

    monkeypatch.setattr(runworker, "WorkflowExecutor", create)
    run_id, token = uuid.uuid4(), uuid.uuid4()
    await runworker.executor_handler("run_workflow", {"run_id": str(run_id)}, token)
    await runworker.executor_handler(
        "resume_workflow", {"run_id": str(run_id), "human_input": {"approved": True}}, token
    )
    assert seen == [{"run_id": run_id, "execution_token": token}] * 2
    executor.execute.assert_awaited_once_with()
    executor.resume.assert_awaited_once_with({"approved": True})
    with pytest.raises(ValueError, match="Unknown"):
        await runworker.executor_handler("unknown_topic", {"run_id": str(run_id)}, token)


def test_heartbeat_checks_expiry_after_waiting_for_run_lock(command, monkeypatch):
    from django.db.models.query import QuerySet

    run, message = command
    token = _claim(1)[0].execution_token
    run.refresh_from_db()
    initial = timezone.now()
    after_lock = run.lease_expires_at + timedelta(seconds=1)
    clock = {"now": initial}
    original_get = QuerySet.get

    def get_after_lock(queryset, *args, **kwargs):
        value = original_get(queryset, *args, **kwargs)
        if queryset.model is WorkflowRun:
            # Simulate time advancing while this select_for_update waited.
            clock["now"] = after_lock
        return value

    monkeypatch.setattr(QuerySet, "get", get_after_lock)
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    assert not _heartbeat(message.id, token)
