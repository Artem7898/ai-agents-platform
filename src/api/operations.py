"""Small, staff-only operational aggregates; no run payloads or credentials."""

from asgiref.sync import sync_to_async
from django.db.models import BigIntegerField, Count, Min, Q, Sum
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from src.runs.models import OutboxMessage, WorkflowRun
from src.runs.schemas import OutboxStatus, RunStatus


@sync_to_async(thread_sensitive=True)
def collect_operations():
    now = timezone.now()
    runs = {status.value: 0 for status in RunStatus}
    runs.update(
        {
            row["status"]: row["count"]
            for row in WorkflowRun.objects.values("status").annotate(count=Count("id"))
        }
    )
    commands = {status.value: 0 for status in OutboxStatus}
    commands.update(
        {
            row["status"]: row["count"]
            for row in OutboxMessage.objects.values("status").annotate(count=Count("id"))
        }
    )
    queue = OutboxMessage.objects.aggregate(
        oldest=Min("created_at", filter=Q(status=OutboxStatus.PENDING.value)),
        expired=Count(
            "id", filter=Q(status=OutboxStatus.PROCESSING.value, lease_expires_at__lte=now)
        ),
    )
    usage = WorkflowRun.objects.aggregate(
        total=Coalesce(
            Sum(Cast("checkpoint__usage__total_tokens", BigIntegerField())),
            0,
            output_field=BigIntegerField(),
        )
    )
    oldest = queue["oldest"]
    return {
        "collected_at": now,
        "run_counts": runs,
        "command_counts": commands,
        "oldest_pending_age_seconds": max(0.0, (now - oldest).total_seconds()) if oldest else 0.0,
        "expired_processing_count": queue["expired"],
        "total_tokens": usage["total"],
    }
