"""Run durable workflow commands independently of the web process."""

import asyncio
import uuid
from typing import Any

import structlog
from django.core.management.base import BaseCommand

from src.agents.llm_service import close_llm_client
from src.runs.executor import WorkflowExecutor
from src.runs.outbox import OutboxWorker

log = structlog.get_logger()


async def executor_handler(topic: str, payload: dict[str, Any], execution_token: uuid.UUID) -> None:
    executor = WorkflowExecutor(
        run_id=uuid.UUID(payload["run_id"]), execution_token=execution_token
    )
    if topic == "run_workflow":
        await executor.execute()
    elif topic == "resume_workflow":
        await executor.resume(payload["human_input"])
    else:
        raise ValueError(f"Unknown outbox topic: {topic}")


class Command(BaseCommand):
    help = "Process leased workflow outbox commands"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--batch-size", type=int, default=5)
        parser.add_argument("--poll-interval", type=float, default=1.0)

    def handle(self, *args, **options):
        if options["batch_size"] < 1 or options["poll_interval"] <= 0:
            raise ValueError("Batch size and poll interval must be positive")
        try:
            asyncio.run(self._run(options))
        except KeyboardInterrupt:
            log.info("outbox_worker_stopped")

    async def _run(self, options):
        log.info("outbox_worker_started")
        try:
            while True:
                processed = await OutboxWorker.process_next_batch(
                    executor_handler, batch_size=options["batch_size"]
                )
                if processed:
                    log.info("outbox_batch_processed", count=processed)
                if options["once"]:
                    return
                await asyncio.sleep(options["poll_interval"])
        finally:
            await close_llm_client()
