"""
Infrastructure Layer: Django ORM модели для домена Runs (Event-Sourced).
"""
import uuid
from django.db import models
from nova import NovaModel, NovaConfig
from .schemas import RunStatus, WorkflowRunSpec, OutboxStatus


class WorkflowRun(NovaModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    workflow = models.ForeignKey(
        "workflows.Workflow",
        on_delete=models.CASCADE,
        related_name="runs"
    )
    status = models.CharField(
        max_length=32,
        choices=[(status.value, status.name) for status in RunStatus],
        default=RunStatus.PENDING.value,
        db_index=True
    )
    input_data = models.JSONField(default=dict)
    output_data = models.JSONField(null=True, blank=True)
    events = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["workflow", "-created_at"]),
        ]
        verbose_name = "Workflow Run"
        verbose_name_plural = "Workflow Runs"

    _nova_config = NovaConfig(
        pydantic_schema=WorkflowRunSpec,
        cache_enabled=False,
        cache_ttl_seconds=300,
        strict_validation=True,
        exclude_from_pydantic=("created_at", "updated_at")
    )

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


# ==============================================================================
# ВНИМАНИЕ: Нулевые отступы! Это отдельная таблица, а не вложенный класс!
# ==============================================================================
class OutboxMessage(NovaModel):
    """
    Инфраструктурная таблица для Outbox Pattern (Этап 2 ТЗ).
    """
    topic = models.CharField(max_length=128, db_index=True)
    payload = models.JSONField()
    status = models.CharField(
        max_length=32,
        choices=[(s.value, s.name) for s in OutboxStatus],
        default=OutboxStatus.PENDING,
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "runs_outbox_message"
        verbose_name = "Outbox Message"
        verbose_name_plural = "Outbox Messages"
        indexes = [
            models.Index(fields=['status', 'created_at'], name='outbox_status_created_idx'),
        ]

    def to_spec(self):
        from .schemas import OutboxMessageSpec
        return OutboxMessageSpec(
            id=self.id,
            topic=self.topic,
            payload=self.payload,
            status=self.status,
            created_at=self.created_at,
            locked_at=self.locked_at,
        )