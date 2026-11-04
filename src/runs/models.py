"""Durable run state, immutable definition snapshots and append-only events."""

import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.utils import timezone

from .schemas import OutboxStatus, RunStatus, WorkflowRunSpec


class WorkflowRun(models.Model):
    if TYPE_CHECKING:
        workflow_id: uuid.UUID
        billing_owner_id: int | None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(
        "workflows.Workflow", on_delete=models.PROTECT, related_name="runs"
    )
    billing_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="llm_workflow_runs",
    )
    status = models.CharField(
        max_length=32,
        choices=[(s.value, s.name) for s in RunStatus],
        default=RunStatus.PENDING.value,
        db_index=True,
    )
    input_data = models.JSONField(default=dict)
    output_data = models.JSONField(null=True, blank=True)
    # Retained for existing databases; new writes go to RunEvent.
    events = models.JSONField(default=list)
    workflow_snapshot = models.JSONField(default=dict)
    agent_snapshots = models.JSONField(default=dict)
    runtime_snapshots = models.JSONField(default=dict)
    checkpoint = models.JSONField(default=dict)
    cancel_requested = models.BooleanField(default=False)
    execution_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["workflow", "-created_at"]),
        ]
        verbose_name = "Workflow Run"
        verbose_name_plural = "Workflow Runs"

    def to_spec(self) -> WorkflowRunSpec:
        return WorkflowRunSpec(
            id=self.id,
            workflow_id=self.workflow_id,
            status=RunStatus(self.status),
            input_data=self.input_data,
            output_data=self.output_data,
            created_at=self.created_at,
        )

    @classmethod
    def from_spec(cls, spec: WorkflowRunSpec, workflow_id: uuid.UUID) -> "WorkflowRun":
        return cls(
            id=spec.id,
            workflow_id=workflow_id,
            status=spec.status.value,
            input_data=spec.input_data,
            output_data=spec.output_data,
        )

    def __str__(self) -> str:
        return f"Run {self.id} ({self.status})"


class RunEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="event_records")
    sequence = models.PositiveIntegerField()
    event_type = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    context_snapshot = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="unique_run_event_sequence")
        ]


class OutboxMessage(models.Model):
    if TYPE_CHECKING:
        run_id: uuid.UUID

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="commands")
    topic = models.CharField(max_length=128, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=32,
        choices=[(s.value, s.name) for s in OutboxStatus],
        default=OutboxStatus.PENDING.value,
        db_index=True,
    )
    dedupe_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    locked_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    execution_token = models.UUIDField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runs_outbox_message"
        indexes = [
            models.Index(fields=["status", "available_at"], name="outbox_status_available_idx")
        ]

    def __str__(self) -> str:
        return f"{self.topic} {self.run_id} ({self.status})"
