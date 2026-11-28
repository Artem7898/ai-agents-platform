"""Atomic destination writes, independently durable before the HTTP response."""

import hashlib
import json
from dataclasses import dataclass

from django.db import transaction

from .models import TriageReceipt, TriageSinkAttempt
from .schemas import SinkSubmissionV1


@dataclass(frozen=True)
class SinkResult:
    receipt: TriageReceipt
    outcome: str


@transaction.atomic
def accept_submission(submission: SinkSubmissionV1) -> SinkResult:
    """Concurrent inserts contend on the database unique key, never a worker cache.

    The conflict is returned as data so its audit row commits before the caller
    emits HTTP 409. The receipt and each accepted attempt commit together.
    """
    payload = submission.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    receipt, created = TriageReceipt.objects.get_or_create(
        idempotency_key=submission.idempotency_key,
        defaults={
            "run_id": submission.run_id,
            "workflow_id": submission.workflow_id,
            "node_id": submission.node_id,
            "ticket_id": submission.ticket_id,
            "payload": payload,
            "payload_hash": payload_hash,
            "category": submission.confirmation.category,
        },
    )
    outcome = "created" if created else "replayed"
    if receipt.payload_hash != payload_hash or receipt.payload != payload:
        outcome = "conflict"
    TriageSinkAttempt.objects.create(
        idempotency_key=submission.idempotency_key,
        payload_hash=payload_hash,
        outcome=outcome,
        receipt=receipt,
    )
    return SinkResult(receipt=receipt, outcome=outcome)
