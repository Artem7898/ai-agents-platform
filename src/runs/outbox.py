"""Leased transactional outbox with fenced acknowledgement and crash recovery.

Delivery is at least once. Tools with external effects must honour the stable
idempotency key supplied by the executor; a process crash cannot roll them back.
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import OutboxMessage, RunEvent, WorkflowRun

log = structlog.get_logger()
Handler = Callable[[str, dict[str, Any], uuid.UUID], Coroutine[Any, Any, None]]
TERMINAL = {"completed", "failed", "cancelled"}


def _lease_seconds() -> float:
    return float(getattr(settings, "OUTBOX_LEASE_SECONDS", 600))


def _locked(queryset):
    if connection.features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update()


def _claim(batch_size: int) -> list[OutboxMessage]:
    now = timezone.now()
    claimed = []
    with transaction.atomic():
        messages = _locked(
            OutboxMessage.objects.filter(
                Q(status="pending", available_at__lte=now)
                | Q(status="processing", lease_expires_at__lte=now)
            ).order_by("available_at", "created_at")
        )
        # Do not pre-claim a batch: each command receives a full lease immediately
        # before execution, rather than expiring while waiting behind another run.
        for message in messages[: max(batch_size * 10, 100)]:
            run = WorkflowRun.objects.select_for_update().get(pk=message.run_id)
            if run.status in TERMINAL:
                message.status = "completed"
                message.save(update_fields=["status", "updated_at"])
                continue
            claimed_at = timezone.now()
            if run.execution_token and run.lease_expires_at and run.lease_expires_at > claimed_at:
                continue
            if message.attempts >= int(getattr(settings, "OUTBOX_MAX_ATTEMPTS", 3)):
                message.status = "failed"
                message.last_error = "Retry limit reached after worker lease expired"
                message.save(update_fields=["status", "last_error", "updated_at"])
                _fail_abandoned_run(run, message.last_error)
                continue
            token = uuid.uuid4()
            expiry = claimed_at + timedelta(seconds=_lease_seconds())
            message.status = "processing"
            message.attempts += 1
            message.locked_at = claimed_at
            message.lease_expires_at = expiry
            message.execution_token = token
            message.save(
                update_fields=[
                    "status",
                    "attempts",
                    "locked_at",
                    "lease_expires_at",
                    "execution_token",
                    "updated_at",
                ]
            )
            run.execution_token = token
            run.lease_expires_at = expiry
            run.save(update_fields=["execution_token", "lease_expires_at", "updated_at"])
            claimed.append(message)
            if len(claimed) >= batch_size:
                break
    return claimed


def _fail_abandoned_run(run: WorkflowRun, reason: str) -> None:
    run.execution_token = None
    run.lease_expires_at = None
    run.status = "failed"
    run.checkpoint = {**run.checkpoint, "error": reason}
    run.save(
        update_fields=["execution_token", "lease_expires_at", "status", "checkpoint", "updated_at"]
    )
    sequence = (RunEvent.objects.filter(run=run).aggregate(last=Max("sequence"))["last"] or 0) + 1
    RunEvent.objects.create(
        run=run,
        sequence=sequence,
        event_type="status_changed",
        payload={"status": "failed", "error": reason},
    )


def _heartbeat(message_id: uuid.UUID, token: uuid.UUID) -> bool:
    with transaction.atomic():
        message = OutboxMessage.objects.select_for_update().get(pk=message_id)
        run = WorkflowRun.objects.select_for_update().get(pk=message.run_id)
        # A row-lock wait can outlive the lease. Check the clock only after both
        # locks are held, otherwise an expired worker could revive itself.
        now = timezone.now()
        expiry = now + timedelta(seconds=_lease_seconds())
        if message.status != "processing" or message.execution_token != token:
            return False
        if not message.lease_expires_at or message.lease_expires_at <= now:
            return False
        if run.execution_token != token or not run.lease_expires_at or run.lease_expires_at <= now:
            return False
        message.lease_expires_at = expiry
        message.save(update_fields=["lease_expires_at", "updated_at"])
        run.lease_expires_at = expiry
        run.save(update_fields=["lease_expires_at", "updated_at"])
        return True


def _finish(message_id: uuid.UUID, token: uuid.UUID, error: str | None) -> bool:
    with transaction.atomic():
        message = OutboxMessage.objects.select_for_update().get(pk=message_id)
        run = WorkflowRun.objects.select_for_update().get(pk=message.run_id)
        if message.execution_token != token or run.execution_token != token:
            return False
        if message.status != "processing":
            return False
        now = timezone.now()
        if (
            not message.lease_expires_at
            or message.lease_expires_at <= now
            or not run.lease_expires_at
            or run.lease_expires_at <= now
        ):
            return False
        if error:
            message.last_error = error[:2000]
            exhausted = message.attempts >= int(getattr(settings, "OUTBOX_MAX_ATTEMPTS", 3))
            message.status = "failed" if exhausted else "pending"
            message.available_at = timezone.now() + timedelta(seconds=min(2**message.attempts, 60))
            if exhausted and run.status not in TERMINAL:
                _fail_abandoned_run(run, "Worker retry limit reached")
        else:
            message.status = "completed"
            message.last_error = ""
        message.execution_token = None
        message.lease_expires_at = None
        message.save(
            update_fields=[
                "status",
                "last_error",
                "available_at",
                "execution_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        # Compare-and-clear: a late worker must never release a successor's lease.
        WorkflowRun.objects.filter(pk=run.pk, execution_token=token).update(
            execution_token=None, lease_expires_at=None
        )
        return True


class OutboxPublisher:
    @staticmethod
    async def publish(topic: str, payload: dict[str, Any]) -> uuid.UUID:
        """Compatibility helper; business writes use services' single transaction."""
        run_id = uuid.UUID(str(payload["run_id"]))
        message = await OutboxMessage.objects.acreate(run_id=run_id, topic=topic, payload=payload)
        return message.id


class OutboxWorker:
    @staticmethod
    async def process_next_batch(executor_coroutine: Handler, batch_size: int = 10) -> int:
        processed = 0
        for _ in range(batch_size):
            messages = await sync_to_async(_claim, thread_sensitive=True)(1)
            if not messages:
                break
            message = messages[0]
            token = message.execution_token
            assert token is not None
            command_task = asyncio.create_task(
                executor_coroutine(message.topic, message.payload, token)
            )

            async def pulse(
                current_message: OutboxMessage = message,
                current_token: uuid.UUID = token,
            ) -> None:
                while True:
                    await asyncio.sleep(max(0.05, _lease_seconds() / 3))
                    alive = await sync_to_async(_heartbeat, thread_sensitive=True)(
                        current_message.id, current_token
                    )
                    if not alive:
                        return

            heartbeat_task = asyncio.create_task(pulse())
            error = None
            lease_lost = False
            try:
                done, _ = await asyncio.wait(
                    {command_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if command_task in done:
                    await command_task
                else:
                    # A stopped heartbeat or a database failure makes ownership
                    # uncertain. Cancel work immediately and let the lease expire;
                    # acknowledging here could release a successor's work.
                    lease_lost = True
                    try:
                        heartbeat_task.result()
                    except Exception as exc:
                        log.warning(
                            "outbox_heartbeat_failed",
                            message_id=str(message.id),
                            error_type=type(exc).__name__,
                        )
            except asyncio.CancelledError:
                # Leave processing until lease expiry on worker shutdown.
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "outbox_task_failed",
                    message_id=str(message.id),
                    run_id=str(message.run_id),
                    attempt=message.attempts,
                    error_type=type(exc).__name__,
                )
            finally:
                for task in (command_task, heartbeat_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(command_task, heartbeat_task, return_exceptions=True)
            if lease_lost:
                log.warning("outbox_lease_lost", message_id=str(message.id))
                continue
            acknowledged = await sync_to_async(_finish, thread_sensitive=True)(
                message.id, token, error
            )
            if acknowledged and error is None:
                processed += 1
        return processed
