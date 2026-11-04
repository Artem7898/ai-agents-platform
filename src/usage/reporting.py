"""Checkpoint projections of the independent invocation ledger."""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.db.models import Count, Q, Sum

from .models import LLMInvocation
from .services import budget_tokens_for_run


@sync_to_async(thread_sensitive=True)
def run_usage(run_id: UUID) -> dict[str, int]:
    rows = LLMInvocation.objects.filter(run_id=run_id).exclude(status="quota_denied")
    values = rows.aggregate(
        calls=Count("pk"),
        reported_total=Sum("total_tokens"),
        unknown_calls=Count("pk", filter=Q(dispatched_at__isnull=False, total_tokens__isnull=True)),
    )
    return {
        "calls": int(values["calls"] or 0),
        "budget_tokens": budget_tokens_for_run(run_id),
        "total_tokens": int(values["reported_total"] or 0),
        "unknown_calls": int(values["unknown_calls"] or 0),
    }
