"""
Transactional Outbox Implementation.
Архитектурное решение: Используем SELECT FOR UPDATE SKIP LOCKED
для безопасного захвата задач в многопоточной (multi-worker) среде.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from django.db import transaction
from django.db.models import F
import structlog

from src.runs.models import OutboxMessage

log = structlog.get_logger()


class OutboxPublisher:
    """Используется в FastAPI контроллерах для постановки задач."""

    @staticmethod
    async def publish(topic: str, payload: dict) -> uuid.UUID:
        """
        Вызывать ТОЛЬКО внутри блока `async with transaction.atomic():`.
        Гарантирует, что OutboxMessage и бизнес-данные сохранятся вместе.
        """
        outbox_msg = OutboxMessage(
            topic=topic,
            payload=payload,
            status="pending"
        )
        await outbox_msg.asave()
        return outbox_msg.id


class OutboxWorker:
    """Используется в management command (фоновый процесс)."""

    @staticmethod
    async def process_next_batch(executor_coroutine, batch_size: int = 10):
        """
        Забирает пачку задач из Outbox, предотвращая захват одной задачи
        несколькими воркерами (Race Condition).
        """
        now = datetime.now(timezone.utc)

        # SELECT ... FOR UPDATE SKIP LOCKED работает ТОЛЬКО в Postgres.
        # Для SQLite (dev окружение) это вызовет ошибку, поэтому для тестов
        # на SQLite мы используем обычный select_for_update().
        try:
            qs = OutboxMessage.objects.filter(
                status="pending"
            ).order_by("created_at")[:batch_size]

            # Пытаемся использовать SKIP LOCKED если БД поддерживает
            qs = qs.select_for_update(skip_locked=True)

        except NotImplementedError:
            # Fallback для SQLite (тесты)
            qs = OutboxMessage.objects.filter(
                status="pending"
            ).order_by("created_at")[:batch_size].select_for_update()

        async with transaction.atomic():
            messages = [msg async for msg in qs]

            if not messages:
                return 0

            # Помечаем как "в работе", чтобы другие воркеры их не видели
            msg_ids = [m.id for m in messages]
            await OutboxMessage.objects.filter(id__in=msg_ids).aupdate(
                status="processing",
                locked_at=now
            )

        processed_count = 0
        for msg in messages:
            try:
                # Вызываем переданную корутину (например, запуск WorkflowExecutor)
                await executor_coroutine(msg.topic, msg.payload)

                await OutboxMessage.objects.filter(id=msg.id).aupdate(status="completed")
                processed_count += 1

            except Exception as e:
                log.error("outbox_task_failed", msg_id=msg.id, topic=msg.topic, error=str(e))
                await OutboxMessage.objects.filter(id=msg.id).aupdate(status="failed")
                # В проде сюда можно добавить Dead Letter Queue (DLQ) логику

        return processed_count