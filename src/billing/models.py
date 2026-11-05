"""Immutable prices and financial evidence, separate from provider cost estimates."""

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.utils import timezone

from .contracts import MAX_MINOR

identifier = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
currency_code = RegexValidator(r"^[A-Z]{3}$")
money_bounds = [MinValueValidator(0), MaxValueValidator(MAX_MINOR)]


class ProtectedRecord(models.Model):
    """Application writes preserve identity; ledger tables have no delete API."""

    frozen_fields: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            if not self._state.adding and self.frozen_fields:
                previous = type(self).objects.using(using).select_for_update().get(pk=self.pk)
                if any(
                    getattr(self, field) != getattr(previous, field) for field in self.frozen_fields
                ):
                    raise ValidationError(
                        "Billing record identity and financial terms are immutable"
                    )
            # Concurrent first creates must converge on database uniqueness rather
            # than fail a racy pre-insert query inside get_or_create.
            self.full_clean(validate_unique=False, validate_constraints=False)
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Billing records cannot be deleted")


class PlanVersion(ProtectedRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=64, validators=[RegexValidator(r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    )
    version = models.CharField(max_length=32, validators=[identifier])
    name = models.CharField(max_length=128)
    price_minor = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_MINOR)]
    )
    currency = models.CharField(max_length=3, validators=[currency_code])
    included_tokens = models.PositiveBigIntegerField()
    included_requests = models.PositiveBigIntegerField()
    overage_per_million_tokens_minor = models.PositiveBigIntegerField(validators=money_bounds)
    overage_per_request_minor = models.PositiveBigIntegerField(validators=money_bounds)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    frozen_fields = (
        "code",
        "version",
        "name",
        "price_minor",
        "currency",
        "included_tokens",
        "included_requests",
        "overage_per_million_tokens_minor",
        "overage_per_request_minor",
        "created_at",
    )

    class Meta:
        ordering = ("code", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=("code", "version"), name="unique_billing_plan_version")
        ]


class BillingAccount(ProtectedRecord):
    if TYPE_CHECKING:
        owner_id: int

    class Mode(models.TextChoices):
        TEST = "test", "Test"
        LIVE = "live", "Live"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="billing_accounts"
    )
    mode = models.CharField(max_length=4, choices=Mode.choices)
    blocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    frozen_fields = ("owner_id", "mode", "created_at")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("owner", "mode"), name="unique_owner_billing_mode")
        ]


class Subscription(ProtectedRecord):
    if TYPE_CHECKING:
        account_id: uuid.UUID
        plan_id: uuid.UUID
        periods: models.Manager["BillingPeriod"]

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment"
        ACTIVE = "active", "Active"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        BillingAccount, on_delete=models.PROTECT, related_name="subscription"
    )
    plan = models.ForeignKey(PlanVersion, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    cancel_at_period_end = models.BooleanField(default=False)
    next_overage_limit_minor = models.PositiveBigIntegerField(default=0, validators=money_bounds)
    anchor_day = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    frozen_fields = ("account_id", "created_at")


class BillingPeriod(ProtectedRecord):
    if TYPE_CHECKING:
        subscription_id: uuid.UUID
        plan_id: uuid.UUID

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="periods")
    plan = models.ForeignKey(PlanVersion, on_delete=models.PROTECT, related_name="periods")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    overage_limit_minor = models.PositiveBigIntegerField(default=0, validators=money_bounds)
    paid_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    used_tokens = models.PositiveBigIntegerField(default=0)
    reserved_tokens = models.PositiveBigIntegerField(default=0)
    used_calls = models.PositiveBigIntegerField(default=0)
    reserved_calls = models.PositiveBigIntegerField(default=0)
    frozen_fields = ("subscription_id", "plan_id", "starts_at", "ends_at", "overage_limit_minor")

    class Meta:
        ordering = ("-starts_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("subscription", "starts_at"), name="unique_subscription_period"
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="billing_period_positive_duration",
            ),
        ]


class Invoice(ProtectedRecord):
    if TYPE_CHECKING:
        period_id: uuid.UUID

    class Kind(models.TextChoices):
        BASE = "base", "Subscription"
        OVERAGE = "overage", "Metered overage"

    class Status(models.TextChoices):
        OPEN = "open", "Awaiting payment"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="invoices")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    amount_minor = models.PositiveBigIntegerField(validators=money_bounds)
    currency = models.CharField(max_length=3, validators=[currency_code])
    raw_overage_minor = models.DecimalField(
        max_digits=50, decimal_places=0, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    settlement_kind = models.CharField(
        max_length=16,
        choices=[
            ("", "Unsettled"),
            ("payment", "Confirmed payment"),
            ("zero_balance", "Zero balance"),
        ],
        default="",
        blank=True,
    )
    issued_at = models.DateTimeField(default=timezone.now, editable=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    frozen_fields = (
        "period_id",
        "kind",
        "amount_minor",
        "currency",
        "raw_overage_minor",
        "issued_at",
    )

    class Meta:
        ordering = ("-issued_at", "id")
        constraints = [
            models.UniqueConstraint(fields=("period", "kind"), name="unique_period_invoice_kind")
        ]


class PaymentReceipt(ProtectedRecord):
    if TYPE_CHECKING:
        invoice_id: uuid.UUID
        recorded_by_id: int

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=128, validators=[identifier])
    merchant_account = models.CharField(max_length=128, validators=[identifier])
    mode = models.CharField(max_length=4, choices=BillingAccount.Mode.choices)
    external_payment_id = models.CharField(max_length=128, validators=[identifier])
    amount_minor = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_MINOR)]
    )
    currency = models.CharField(max_length=3, validators=[currency_code])
    evidence_ref = models.CharField(max_length=128, validators=[identifier])
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_billing_payments"
    )
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)
    frozen_fields = (
        "invoice_id",
        "provider",
        "merchant_account",
        "mode",
        "external_payment_id",
        "amount_minor",
        "currency",
        "evidence_ref",
        "recorded_by_id",
        "recorded_at",
    )

    class Meta:
        ordering = ("-recorded_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "merchant_account", "mode", "external_payment_id"),
                name="unique_billing_payment_evidence",
            ),
            models.UniqueConstraint(fields=("invoice",), name="unique_confirmed_invoice_payment"),
        ]

    def clean(self):
        super().clean()
        if self.invoice_id:
            invoice = self.invoice
            if self.amount_minor != invoice.amount_minor or self.currency != invoice.currency:
                raise ValidationError("Payment must match the exact invoice amount and currency")
            if self.mode != invoice.period.subscription.account.mode:
                raise ValidationError("Payment must match the invoice environment")
