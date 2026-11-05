"""Personal console aggregates, derived from persisted records only.

Staff receives the same personal dashboard as any other owner. Operational
staff-wide inspection remains a separate API. The dashboard never probes an LLM
or external knowledge service, and does not infer worker uptime or model latency.
"""

from collections import defaultdict
from datetime import UTC, timedelta
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import BigIntegerField, Count, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncHour
from django.utils import timezone

from src.agents.models import Agent
from src.billing.models import BillingPeriod, Invoice, Subscription
from src.runs.models import OutboxMessage, WorkflowRun
from src.usage.models import LLMInvocation
from src.workflows.models import Workflow

# A chart may inspect only this many distinct invocation-node groups. Its exact
# aggregate metric remains a separate SQL query; an incomplete chart is explicit.
MAX_USAGE_GROUPS = 2000
_FINISHED = {"completed", "failed", "cancelled"}


def _agent_for_node(run, node_id):
    definition = run.workflow_snapshot if isinstance(run.workflow_snapshot, dict) else {}
    nodes = definition.get("nodes", [])
    if not isinstance(nodes, list):
        return None, "Unassigned"
    agent_id = None
    for node in nodes:
        if isinstance(node, dict) and node.get("id") == node_id:
            try:
                agent_id = str(UUID(str(node.get("agent_id"))))
            except (TypeError, ValueError, AttributeError):
                pass
            break
    if agent_id is None:
        return None, "Unassigned"
    snapshots = run.agent_snapshots if isinstance(run.agent_snapshots, dict) else {}
    snapshot = snapshots.get(agent_id, {})
    spec = snapshot.get("spec", {}) if isinstance(snapshot, dict) else {}
    name = spec.get("name") if isinstance(spec, dict) else None
    return agent_id, name[:128] if isinstance(name, str) and name else "Unknown agent"


def _token_usage(calls):
    grouped = list(
        calls.values("run_id", "node_id")
        .annotate(
            tokens=Coalesce(Sum("total_tokens"), Value(0), output_field=BigIntegerField()),
            unknown_calls=Count("id", filter=Q(total_tokens__isnull=True)),
        )
        .order_by("-tokens", "run_id", "node_id")[: MAX_USAGE_GROUPS + 1]
    )
    complete = len(grouped) <= MAX_USAGE_GROUPS
    by_run = defaultdict(list)
    for group in grouped[:MAX_USAGE_GROUPS]:
        by_run[group["run_id"]].append(group)
    result = {}
    # A frozen definition can be large. Stream one run's snapshots at a time,
    # retaining only small chart rows, never prompts or runtime credentials.
    runs = (
        WorkflowRun.objects.filter(pk__in=by_run)
        .only("id", "workflow_snapshot", "agent_snapshots")
        .iterator(chunk_size=1)
    )
    for run in runs:
        for group in by_run[run.pk]:
            agent_id, name = _agent_for_node(run, group["node_id"])
            key = agent_id or "unassigned"
            if key not in result:
                result[key] = {
                    "agent_id": agent_id,
                    "agent_name": name,
                    "tokens": 0,
                    "unknown_calls": 0,
                }
            result[key]["tokens"] += group["tokens"]
            result[key]["unknown_calls"] += group["unknown_calls"]
    ordered = sorted(result.values(), key=lambda row: (-row["tokens"], row["agent_name"]))
    if len(ordered) > 8:
        tail = ordered[7:]
        ordered = ordered[:7] + [
            {
                "agent_id": None,
                "agent_name": "Other agents",
                "tokens": sum(row["tokens"] for row in tail),
                "unknown_calls": sum(row["unknown_calls"] for row in tail),
            }
        ]
    return ordered, complete


def _run_costs(run_ids, owner_id):
    result = {}
    for row in (
        LLMInvocation.objects.filter(run_id__in=run_ids, owner_id=owner_id)
        .exclude(status="quota_denied")
        .values("run_id")
        .annotate(
            amount=Sum("cost"),
            unknown=Count("id", filter=Q(cost__isnull=True) | Q(currency__isnull=True)),
            currencies=Count("currency", distinct=True),
            currency=Max("currency"),
        )
    ):
        single_currency = row["currencies"] == 1
        result[row["run_id"]] = {
            "cost": str(row["amount"])
            if single_currency and row["unknown"] == 0 and isinstance(row["amount"], Decimal)
            else None,
            "currency": row["currency"] if single_currency else None,
        }
    return result


def _recent_runs(runs, now, owner_id):
    recent = list(
        runs.select_related("workflow")
        .only(
            "id",
            "status",
            "created_at",
            "updated_at",
            "workflow_snapshot",
            "workflow__owner_id",
            "workflow__name",
        )
        .order_by("-created_at", "-id")[:8]
    )
    costs = _run_costs([run.pk for run in recent], owner_id)
    result = []
    for run in recent:
        snapshot = run.workflow_snapshot if isinstance(run.workflow_snapshot, dict) else {}
        name = snapshot.get("name")
        if not isinstance(name, str) or not name:
            name = run.workflow.name if run.workflow.owner_id == owner_id else "Workflow"
        end = run.updated_at if run.status in _FINISHED else now
        result.append(
            {
                "id": str(run.pk),
                "workflow_name": name[:256],
                "status": run.status,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
                "duration_seconds": max(0, (end - run.created_at).total_seconds()),
                **costs.get(run.pk, {"cost": None, "currency": None}),
            }
        )
    return result


def _active_agents(owner_id, runs):
    result = []
    for agent in Agent.objects.filter(owner_id=owner_id, status="active").order_by(
        "-updated_at", "id"
    )[:5]:
        counts = runs.filter(agent_snapshots__has_key=str(agent.pk)).aggregate(
            running=Count("id", filter=Q(status="running")),
            waiting=Count("id", filter=Q(status="waiting_human")),
        )
        result.append(
            {
                "id": str(agent.pk),
                "name": agent.name,
                "model_name": agent.model_name,
                "status": agent.status,
                "tools_count": len(agent.tool_ids) if isinstance(agent.tool_ids, list) else 0,
                "active_runs": counts["running"],
                "waiting_runs": counts["waiting"],
            }
        )
    return result


def _workflow_preview(owner_id):
    workflow = (
        Workflow.objects.filter(owner_id=owner_id)
        .only("id", "name", "nodes", "edges")
        .order_by("-updated_at", "id")
        .first()
    )
    if workflow is None:
        return None
    nodes = workflow.nodes if isinstance(workflow.nodes, list) else []
    edges = workflow.edges if isinstance(workflow.edges, list) else []
    return {
        "id": str(workflow.pk),
        "name": workflow.name,
        # Overview graphs need topology, not tool arguments or private prompts.
        "nodes": [
            {key: node.get(key) for key in ("id", "kind", "agent_id")}
            for node in nodes
            if isinstance(node, dict)
        ],
        "edges": [
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "condition": edge.get("condition") if type(edge.get("condition")) is bool else None,
            }
            for edge in edges
            if isinstance(edge, dict)
        ],
    }


def _billing(owner_id, now):
    subscription = (
        Subscription.objects.select_related("plan", "account")
        .filter(account__owner_id=owner_id, account__mode=settings.BILLING_MODE)
        .first()
    )
    result = {
        "enabled": settings.BILLING_ENABLED,
        "mode": settings.BILLING_MODE,
        "subscription_status": None,
        "overage_limit_minor": None,
        "currency": None,
    }
    if subscription is None:
        return result
    period = (
        BillingPeriod.objects.select_related("plan")
        .filter(subscription=subscription, starts_at__lte=now, ends_at__gt=now)
        .order_by("-starts_at")
        .first()
    )
    status = subscription.status
    if subscription.account.blocked:
        status = "blocked"
    elif status == "active":
        if period is None or period.paid_at is None or period.closed_at is not None:
            status = "expired"
        elif subscription.cancel_at_period_end:
            status = "canceling"
    result.update(
        subscription_status=status,
        overage_limit_minor=period.overage_limit_minor if period else None,
        currency=period.plan.currency if period else subscription.plan.currency,
    )
    return result


def dashboard(user) -> dict:
    """Collect personal, bounded dashboard data without making external requests."""
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    now = timezone.now().astimezone(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    runs = WorkflowRun.objects.filter(billing_owner_id=user.pk)
    calls = LLMInvocation.objects.filter(
        owner_id=user.pk,
        run__billing_owner_id=user.pk,
        started_at__gte=today,
        started_at__lte=now,
        dispatched_at__isnull=False,
    ).exclude(status="quota_denied")
    usage = calls.aggregate(
        tokens=Coalesce(Sum("total_tokens"), Value(0), output_field=BigIntegerField()),
        unknown=Count("id", filter=Q(total_tokens__isnull=True)),
    )
    tokens, complete = _token_usage(calls)
    activity = [
        {
            "hour": (first_hour + timedelta(hours=index)).isoformat(),
            "label": (first_hour + timedelta(hours=index)).strftime("%H:%M"),
            "completed": 0,
            "failed": 0,
        }
        for index in range(24)
    ]
    positions = {row["hour"]: row for row in activity}
    for row in (
        runs.filter(
            status__in=("completed", "failed"), updated_at__gte=first_hour, updated_at__lte=now
        )
        .annotate(hour=TruncHour("updated_at", tzinfo=UTC))
        .values("hour", "status")
        .annotate(count=Count("id"))
        .order_by("hour")
    ):
        positions[row["hour"].isoformat()][row["status"]] = row["count"]
    failed = runs.filter(
        status="failed", updated_at__gte=now - timedelta(hours=24), updated_at__lte=now
    ).count()
    waiting = runs.filter(status="waiting_human").count()
    alerts = []
    if waiting:
        alerts.append(
            {
                "id": "human-approval",
                "kind": "warning",
                "title": f"{waiting} runs need approval",
                "description": "Open waiting runs to review the saved request.",
                "href": "/runs?status=waiting_human",
            }
        )
    if failed:
        alerts.append(
            {
                "id": "failed-runs",
                "kind": "error",
                "title": f"{failed} failed runs in 24 hours",
                "description": "Inspect the trace before retrying external effects.",
                "href": "/runs?status=failed",
            }
        )
    if usage["unknown"]:
        alerts.append(
            {
                "id": "unknown-usage",
                "kind": "warning",
                "title": f"{usage['unknown']} calls have unknown usage",
                "description": "Reported totals remain unknown until accounting is reconciled.",
                "href": "/observability",
            }
        )
    due = Invoice.objects.filter(
        period__subscription__account__owner_id=user.pk,
        period__subscription__account__mode=settings.BILLING_MODE,
        kind="overage",
        status="open",
        amount_minor__gt=0,
    ).count()
    if due:
        alerts.append(
            {
                "id": "overage-due",
                "kind": "warning",
                "title": f"{due} overage invoices await payment",
                "description": "Existing overage can prevent new paid admissions.",
                "href": "/billing",
            }
        )
    return {
        "generated_at": now.isoformat(),
        "timezone": "UTC",
        "scope": "personal",
        "metrics": {
            "active_runs": runs.filter(status="running").count(),
            "queued_tasks": OutboxMessage.objects.filter(
                run__billing_owner_id=user.pk, status="pending"
            ).count(),
            "tokens_today": None if usage["unknown"] else usage["tokens"],
            "known_tokens_today": usage["tokens"],
            "unknown_usage_calls_today": usage["unknown"],
            "errors_24h": failed,
            "system_health": {
                "status": "ready",
                "label": "API and database ready",
                "uptime_percent": None,
            },
        },
        "activity": activity,
        "token_usage": tokens,
        "token_usage_complete": complete,
        "active_agents": _active_agents(user.pk, runs),
        "recent_runs": _recent_runs(runs, now, user.pk),
        "workflow_preview": _workflow_preview(user.pk),
        "alerts": alerts,
        "knowledge": {
            "configured": bool(settings.KNOWLEDGE_BASE_SEARCH_URL),
            "index_status": "not_connected",
            "documents_count": None,
            "memory_status": "not_available",
        },
        "billing": _billing(user.pk, now),
    }
