"""
Background worker for Outbox Pattern.
Запуск: python manage.py runworker
"""
import asyncio
import django
import os
from django.core.management.base import BaseCommand

# Инициализация Django окружения для standalone скрипта
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

from src.runs.outbox import OutboxWorker
from src.runs.executor import WorkflowExecutor
import structlog

log = structlog.get_logger()


async def executor_handler(topic: str, payload: dict):
    """Роутинг топиков Outbox на реальные сервисы."""
    if topic == "run_workflow":
        run_id = payload.get("run_id")
        executor = WorkflowExecutor()
        # Вызываем существующий метод executor-а
        await executor.execute(run_id)
    else:
        log.warning("outbox_unknown_topic", topic=topic)


class Command(BaseCommand):
    help = 'Runs the background Outbox worker to process workflow tasks'

    def handle(self, *args, **options):
        log.info("outbox_worker_started")
        worker = OutboxWorker()

        try:
            # Бесконечный цикл воркера
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            while True:
                # Опрашиваем БД каждую секунду (в проде можно сделать 0.1с)
                processed = loop.run_until_complete(
                    worker.process_next_batch(executor_handler, batch_size=5)
                )
                if processed > 0:
                    log.info("outbox_batch_processed", count=processed)

                loop.run_until_complete(asyncio.sleep(1.0))

        except KeyboardInterrupt:
            log.info("outbox_worker_stopped")