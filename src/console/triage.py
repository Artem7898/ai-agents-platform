"""Small, owner-scoped browser projections and commands for ticket triage."""

from django.conf import settings
from django.shortcuts import get_object_or_404
from pydantic import ValidationError
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from src.runs.models import OutboxMessage, WorkflowRun
from src.runs.services import enqueue_resume
from src.triage.models import TriageReceipt
from src.triage.schemas import CATEGORIES, ConfirmedCategoryV1, TicketClassificationV1

from .api import ConsoleAPIView, ConsoleCommandError, _command, _idempotency


def _runs(user):
    # This dedicated inbox is personal even for staff. The frozen contract, not
    # the mutable workflow name or current definition, identifies triage runs.
    return WorkflowRun.objects.filter(
        workflow__owner=user,
        output_schema_snapshots__classify__id="ticket_classification",
        output_schema_snapshots__classify__version=1,
    )


def _checkpoint(run):
    return run.checkpoint if isinstance(run.checkpoint, dict) else {}


def _node_result(run, node_id):
    context = _checkpoint(run).get("context")
    nodes = context.get("nodes") if isinstance(context, dict) else None
    node = nodes.get(node_id) if isinstance(nodes, dict) else None
    return node.get("result") if isinstance(node, dict) else None


def _validated(model, value):
    try:
        return model.model_validate(value).model_dump(mode="json")
    except ValidationError:
        return None


def _classification(run):
    completed = _checkpoint(run).get("completed", [])
    if not isinstance(completed, list) or "classify" not in completed:
        return None
    return _validated(TicketClassificationV1, _node_result(run, "classify"))


def _pending_approvals(runs):
    pending = {}
    commands = OutboxMessage.objects.filter(
        run_id__in=[run.pk for run in runs],
        topic="resume_workflow",
        status__in=["pending", "processing"],
    ).order_by("-created_at")
    for command in commands:
        payload = command.payload if isinstance(command.payload, dict) else {}
        human_input = payload.get("human_input")
        if isinstance(human_input, dict) and human_input.get("node_id") == "approval":
            pending.setdefault(command.run_id, human_input.get("input"))
    return pending


def _row(run, pending, receipt):
    checkpoint = _checkpoint(run)
    inputs = run.input_data if isinstance(run.input_data, dict) else {}
    classification = _classification(run)
    confirmation = _validated(ConfirmedCategoryV1, _node_result(run, "approval"))
    if confirmation is None and run.pk in pending:
        confirmation = _validated(ConfirmedCategoryV1, pending[run.pk])
    waiting = checkpoint.get("waiting", [])
    return {
        "id": str(run.pk),
        "status": run.status,
        "ticket_id": inputs.get("ticket_id") if isinstance(inputs.get("ticket_id"), str) else "",
        "text": inputs.get("text") if isinstance(inputs.get("text"), str) else "",
        "classification": classification,
        "confirmation": confirmation,
        "waiting_for_approval": (
            run.status == "waiting_human"
            and not run.cancel_requested
            and isinstance(waiting, list)
            and "approval" in waiting
            and classification is not None
            and confirmation is None
            and run.pk not in pending
        ),
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "receipt": receipt.as_receipt() if receipt is not None else None,
    }


class TriageRunsView(ConsoleAPIView):
    def get(self, request):
        enabled = bool(settings.DEMO_TRIAGE_ENABLED)
        body = {"enabled": enabled, "categories": list(CATEGORIES), "results": []}
        if not enabled:
            return Response(body)
        runs = list(_runs(request.user).order_by("-created_at", "-pk")[:50])
        pending = _pending_approvals(runs)
        receipts = {}
        for receipt in TriageReceipt.objects.filter(run_id__in=[run.pk for run in runs]):
            receipts.setdefault(receipt.run_id, receipt)
        body["results"] = [_row(run, pending, receipts.get(run.pk)) for run in runs]
        return Response(body)


class TriageConfirmView(ConsoleAPIView):
    def post(self, request, run_id):
        if not settings.DEMO_TRIAGE_ENABLED:
            raise NotFound()
        run = get_object_or_404(_runs(request.user), pk=run_id)
        try:
            confirmation = ConfirmedCategoryV1.model_validate(request.data)
        except ValidationError:
            raise ConsoleCommandError("invalid_command", 422) from None
        if _classification(run) is None:
            raise ConsoleCommandError("command_rejected", 409)
        # enqueue_resume owns state checks and deduplication, including retries
        # with the same key after the worker has completed the run.
        _command(
            enqueue_resume,
            run.pk,
            {"node_id": "approval", "input": confirmation.model_dump(mode="json")},
            request.user,
            idempotency_key=_idempotency(request),
        )
        return Response({"run_id": str(run.pk), "status": "queued"}, status=202)
