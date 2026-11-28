"""Authenticated local demo sink, separate from workflow checkpoint transactions."""

import time

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from pydantic import ValidationError

from .schemas import SinkSubmissionV1
from .services import accept_submission
from .tools import is_valid_sink_token


def _commit_barrier(run_id: str) -> None:
    """Optional targeted fault window; it is disabled unless explicitly configured.

    The supervisor observes the committed receipt before killing the worker.
    A retry gets the original receipt immediately, since only creation pauses.
    """
    target = str(getattr(settings, "DEMO_TRIAGE_COMMIT_BARRIER_RUN_ID", ""))
    duration = getattr(settings, "DEMO_TRIAGE_COMMIT_BARRIER_SECONDS", 0)
    if target == run_id and type(duration) in (int, float) and 0 < duration <= 60:
        time.sleep(duration)


@transaction.non_atomic_requests
@csrf_exempt
@require_POST
def sink(request: HttpRequest) -> JsonResponse:
    if not getattr(settings, "DEMO_TRIAGE_ENABLED", False):
        return JsonResponse({"error": "Ticket triage demo is disabled"}, status=404)
    if not is_valid_sink_token(request.headers.get("X-Demo-Triage-Token", "")):
        return JsonResponse({"error": "Invalid sink credentials"}, status=403)
    body = request.read(16_385)
    if len(body) > 16_384:
        return JsonResponse({"error": "Submission exceeds the demo size limit"}, status=413)
    try:
        submission = SinkSubmissionV1.model_validate_json(body)
    except ValidationError:
        return JsonResponse({"error": "Invalid confirmed ticket submission"}, status=400)
    if request.headers.get("Idempotency-Key") != submission.idempotency_key:
        return JsonResponse(
            {"error": "The idempotency header must match the submission"}, status=400
        )
    result = accept_submission(submission)
    if result.outcome == "conflict":
        return JsonResponse(
            {"error": "Idempotency key already belongs to another payload"}, status=409
        )
    # accept_submission's atomic block has exited: the sink commit is durable
    # before the response or the demo fault window. No worker transaction owns it.
    if result.outcome == "created":
        _commit_barrier(str(submission.run_id))
    return JsonResponse(
        result.receipt.as_receipt(), status=201 if result.outcome == "created" else 200
    )
