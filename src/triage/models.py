"""The demo destination ledger; its unique key is the final deduplication boundary."""

import uuid

from django.db import models


class TriageReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=512, unique=True)
    run_id = models.UUIDField(db_index=True)
    workflow_id = models.UUIDField()
    node_id = models.CharField(max_length=128)
    ticket_id = models.CharField(max_length=64)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    category = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def as_receipt(self) -> dict:
        return {
            "receipt_id": str(self.id),
            "idempotency_key": self.idempotency_key,
            "run_id": str(self.run_id),
            "ticket_id": self.ticket_id,
            "category": self.category,
            "payload_hash": self.payload_hash,
            "created_at": self.created_at.isoformat(),
        }


class TriageSinkAttempt(models.Model):
    idempotency_key = models.CharField(max_length=512, db_index=True)
    payload_hash = models.CharField(max_length=64)
    outcome = models.CharField(
        max_length=16,
        choices=[("created", "Created"), ("replayed", "Replayed"), ("conflict", "Conflict")],
    )
    receipt = models.ForeignKey(TriageReceipt, on_delete=models.PROTECT, related_name="attempts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
