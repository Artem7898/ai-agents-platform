"""Separate financial records: workflow checkpoints never reset these counters."""

import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.utils import timezone


class OwnerQuota(models.Model):
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="llm_quota"
    )
    token_limit = models.PositiveBigIntegerField(null=True, blank=True)
    request_limit = models.PositiveBigIntegerField(null=True, blank=True)
    period_seconds = models.PositiveIntegerField(default=86400, validators=[MinValueValidator(1)])
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            if not self._state.adding:
                previous = type(self).objects.using(using).select_for_update().get(pk=self.pk)
                if any(
                    getattr(self, field) != getattr(previous, field)
                    for field in ("owner_id", "period_seconds", "created_at")
                ):
                    raise ValidationError("The quota owner, period and anchor are immutable")
            self.full_clean()
            return super().save(*args, **kwargs)


class QuotaWindow(models.Model):
    quota = models.ForeignKey(OwnerQuota, on_delete=models.PROTECT, related_name="windows")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    used_tokens = models.PositiveBigIntegerField(default=0)
    reserved_tokens = models.PositiveBigIntegerField(default=0)
    used_calls = models.PositiveBigIntegerField(default=0)
    reserved_calls = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ("-starts_at",)
        constraints = [
            models.UniqueConstraint(fields=("quota", "starts_at"), name="unique_quota_window"),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="quota_window_positive_period",
            ),
        ]


class LLMRate(models.Model):
    """Append a new version when a tariff changes; existing calls keep a snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runtime = models.ForeignKey(
        "runtimes.LLMRuntime", null=True, blank=True, on_delete=models.PROTECT, related_name="rates"
    )
    model_name = models.CharField(max_length=255)
    version = models.CharField(max_length=64)
    effective_from = models.DateTimeField(default=timezone.now)
    currency = models.CharField(max_length=3, validators=[RegexValidator(r"^[A-Z]{3}$")])
    input_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(0)]
    )
    output_per_million = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(0)]
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-effective_from", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("runtime", "model_name", "version"),
                condition=models.Q(runtime__isnull=False),
                name="unique_llm_rate_version",
            ),
            models.UniqueConstraint(
                fields=("model_name", "version"),
                condition=models.Q(runtime__isnull=True),
                name="unique_global_llm_rate_version",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Rates are immutable; create a new tariff version")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Rates are immutable")


class LLMInvocation(models.Model):
    if TYPE_CHECKING:
        run_id: uuid.UUID
        quota_window_id: int | None
        billing_period_id: uuid.UUID | None
        runtime_id: uuid.UUID | None

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        DISPATCHED = "dispatched", "Dispatched"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        UNKNOWN = "unknown", "Unknown"
        QUOTA_DENIED = "quota_denied", "Quota denied"

    class UsageStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        PARTIAL = "partial", "Partial"
        REPORTED = "reported", "Reported"
        RECONCILED = "reconciled", "Reconciled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="llm_invocations",
    )
    run = models.ForeignKey(
        "runs.WorkflowRun", on_delete=models.PROTECT, related_name="llm_invocations"
    )
    node_id = models.CharField(max_length=255)
    logical_call = models.PositiveIntegerField()
    attempt = models.PositiveIntegerField()
    runtime = models.ForeignKey(
        "runtimes.LLMRuntime",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invocations",
    )
    runtime_revision = models.PositiveIntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=255)
    provider = models.CharField(max_length=32)
    operation = models.CharField(max_length=32)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RESERVED, db_index=True
    )
    quota_window = models.ForeignKey(
        QuotaWindow, null=True, blank=True, on_delete=models.PROTECT, related_name="invocations"
    )
    billing_period = models.ForeignKey(
        "billing.BillingPeriod",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invocations",
    )
    reserved_tokens = models.PositiveBigIntegerField()
    max_output_tokens = models.PositiveIntegerField()
    # Active holds survive cancellation, crash and missing usage after dispatch.
    reservation_active = models.BooleanField(default=True)
    prompt_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    usage_status = models.CharField(
        max_length=16, choices=UsageStatus.choices, default=UsageStatus.UNKNOWN
    )
    provider_request_id = models.CharField(max_length=128, null=True, blank=True)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    execution_token = models.UUIDField(null=True, blank=True)
    rate = models.ForeignKey(
        LLMRate, null=True, blank=True, on_delete=models.PROTECT, related_name="invocations"
    )
    rate_version = models.CharField(max_length=64, null=True, blank=True)
    currency = models.CharField(max_length=3, null=True, blank=True)
    input_per_million = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    output_per_million = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    cost = models.DecimalField(max_digits=50, decimal_places=18, null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "node_id", "logical_call", "attempt"),
                name="unique_llm_invocation_attempt",
            )
        ]
        indexes = [
            models.Index(fields=("owner", "-started_at")),
            models.Index(fields=("run", "node_id", "logical_call")),
        ]
