"""A scoped real outbox worker used only by the proof supervisor."""

import asyncio
import uuid

from django.core.management.base import BaseCommand, CommandError

from src.agents.llm_service import close_llm_client
from src.runs.management.commands.runworker import executor_handler
from src.runs.outbox import OutboxWorker


class Command(BaseCommand):
    help = "Process one real outbox command, restricted to explicitly supplied demo run IDs"

    def add_arguments(self, parser):
        parser.add_argument("--run-id", required=True, action="append", type=uuid.UUID)

    def handle(self, *args, **options):
        if not options["run_id"]:
            raise CommandError("At least one explicit run ID is required")
        asyncio.run(self._run(options["run_id"]))

    async def _run(self, run_ids):
        try:
            processed = await OutboxWorker.process_next_batch(
                executor_handler, batch_size=1, run_ids=run_ids
            )
            self.stdout.write(f"processed={processed}")
        finally:
            await close_llm_client()
