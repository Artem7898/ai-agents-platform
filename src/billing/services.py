"""Provider-neutral subscriptions, payment evidence and capped overage invoices.

The payment entry point is an explicit staff attestation, not an unauthenticated
provider callback. No service in this module calls a payment processor. Financial
mutations serialize on the account; usage takes that same lock after its run lock.
"""

import calendar
import re
from datetime import UTC, datetime
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .contracts import MAX_MINOR, BillingError
from .entitlements import has_outstanding_debt, overage_minor
from .models import (
    BillingAccount,
    BillingPeriod,
    Invoice,
    PaymentReceipt,
    PlanVersion,
    Subscription,
)

_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _mode() -> str:
    mode = settings.BILLING_MODE
    if mode not in {"test", "live"}:
        raise BillingError("invalid_configuration")
    return mode


def _enabled() -> None:
    if not settings.BILLING_ENABLED:
        raise BillingError("billing_disabled")


def _minor(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_MINOR:
        raise BillingError("invalid_amount")
    return value


def _now() -> datetime:
    return timezone.now().astimezone(UTC)


def _next_month(starts_at: datetime, anchor_day: int) -> datetime:
    """Clamp February without drifting a January-31 renewal to March-28."""
    month = starts_at.month % 12 + 1
    year = starts_at.year + (starts_at.month == 12)
    try:
        day = min(anchor_day, calendar.monthrange(year, month)[1])
        return starts_at.replace(year=year, month=month, day=day)
    except (ValueError, OverflowError):
        raise BillingError("invalid_period") from None


def _account(owner_id: int, *, create: bool = False) -> BillingAccount:
    if type(owner_id) is not int or owner_id < 1:
        raise BillingError("not_found")
    mode = _mode()
    if create:
        if not get_user_model().objects.filter(pk=owner_id, is_active=True).exists():
            raise BillingError("not_found")
        # get_or_create handles a competing first insert through the DB constraint.
        BillingAccount.objects.get_or_create(owner_id=owner_id, mode=mode)
    account = (
        BillingAccount.objects.select_for_update().filter(owner_id=owner_id, mode=mode).first()
    )
    if account is None:
        raise BillingError("not_found")
    return account


def _subscription(owner_id: int, subscription_id: UUID) -> tuple[BillingAccount, Subscription]:
    account = _account(owner_id)
    subscription = Subscription.objects.filter(pk=subscription_id, account=account).first()
    if subscription is None:
        raise BillingError("not_found")
    return account, subscription


def _new_period(subscription: Subscription, starts_at: datetime) -> BillingPeriod:
    try:
        with transaction.atomic():
            period = BillingPeriod.objects.create(
                subscription=subscription,
                plan=subscription.plan,
                starts_at=starts_at,
                ends_at=_next_month(starts_at, subscription.anchor_day),
                overage_limit_minor=subscription.next_overage_limit_minor,
            )
            Invoice.objects.create(
                period=period,
                kind="base",
                amount_minor=subscription.plan.price_minor,
                currency=subscription.plan.currency,
            )
    except IntegrityError:
        raise BillingError("period_conflict") from None
    return period


def _can_start_period(account: BillingAccount, now: datetime) -> None:
    if account.blocked:
        raise BillingError("account_blocked")
    if has_outstanding_debt(account, now):
        raise BillingError("outstanding_overage")


@transaction.atomic
def start_subscription(
    *, owner_id: int, plan_id: UUID, overage_limit_minor: int = 0
) -> BillingPeriod:
    """Create a payment request; access begins only after its payment is recorded."""
    _enabled()
    cap = _minor(overage_limit_minor)
    account = _account(owner_id, create=True)
    plan = PlanVersion.objects.select_for_update().filter(pk=plan_id, enabled=True).first()
    if plan is None:
        raise BillingError("invalid_plan")
    now = _now()
    subscription = Subscription.objects.filter(account=account).first()
    if subscription is not None:
        latest = subscription.periods.order_by("-starts_at", "-id").first()
        paid_through = subscription.periods.filter(paid_at__isnull=False, ends_at__gt=now).exists()
        if (
            subscription.status == "canceled" or subscription.cancel_at_period_end
        ) and not paid_through:
            # A new lifecycle starts now; old periods keep all of their frozen terms.
            _can_start_period(account, now)
            subscription.plan = plan
            subscription.status = "pending"
            subscription.cancel_at_period_end = False
            subscription.next_overage_limit_minor = cap
            subscription.anchor_day = now.day
            subscription.save()
            return _new_period(subscription, now)
        if subscription.cancel_at_period_end or subscription.status == "canceled":
            raise BillingError("subscription_canceled")
        if subscription.plan_id != plan.pk or latest is None or latest.overage_limit_minor != cap:
            raise BillingError("subscription_conflict")
        return latest
    _can_start_period(account, now)
    subscription = Subscription.objects.create(
        account=account,
        plan=plan,
        next_overage_limit_minor=cap,
        anchor_day=now.day,
    )
    return _new_period(subscription, now)


@transaction.atomic
def renew_subscription(*, owner_id: int, subscription_id: UUID) -> BillingPeriod:
    """Prepare at most one future invoice and never charge for an unpaid gap."""
    _enabled()
    account, subscription = _subscription(owner_id, subscription_id)
    if subscription.cancel_at_period_end or subscription.status == "canceled":
        raise BillingError("subscription_canceled")
    now = _now()
    latest = subscription.periods.select_for_update().order_by("-starts_at", "-id").first()
    if latest is None:
        raise BillingError("subscription_conflict")
    if latest.paid_at is None and latest.ends_at > now:
        return latest
    if latest.paid_at is not None and latest.starts_at > now:
        return latest
    _can_start_period(account, now)
    plan = PlanVersion.objects.select_for_update().get(pk=subscription.plan_id)
    if not plan.enabled:
        raise BillingError("invalid_plan")
    if latest.paid_at is None:
        Invoice.objects.filter(period=latest, kind="base", status="open").update(status="void")
    if latest.ends_at < now:
        starts_at = now
        subscription.anchor_day = now.day
        subscription.save(update_fields=["anchor_day"])
    else:
        starts_at = latest.ends_at
    return _new_period(subscription, starts_at)


@transaction.atomic
def cancel_subscription(*, owner_id: int, subscription_id: UUID) -> Subscription:
    """Stop new renewals while honoring every already-paid period."""
    _, subscription = _subscription(owner_id, subscription_id)
    subscription.cancel_at_period_end = True
    if not subscription.periods.filter(paid_at__isnull=False, ends_at__gt=_now()).exists():
        subscription.status = "canceled"
    subscription.save(update_fields=["cancel_at_period_end", "status"])
    Invoice.objects.filter(period__subscription=subscription, kind="base", status="open").update(
        status="void"
    )
    return subscription


@transaction.atomic
def set_next_overage_limit(
    *, owner_id: int, subscription_id: UUID, overage_limit_minor: int
) -> Subscription:
    """Change future consent only; the current period and its bill cannot change."""
    cap = _minor(overage_limit_minor)
    _, subscription = _subscription(owner_id, subscription_id)
    subscription.next_overage_limit_minor = cap
    subscription.save(update_fields=["next_overage_limit_minor"])
    return subscription


@transaction.atomic
def record_payment(
    *,
    invoice_id: UUID,
    actor_id: int,
    provider: str,
    merchant_account: str,
    external_payment_id: str,
    amount_minor: int,
    currency: str,
    evidence_ref: str,
) -> PaymentReceipt:
    """Record a staff-confirmed exact payment once, in its immutable namespace."""
    if not get_user_model().objects.filter(pk=actor_id, is_active=True, is_staff=True).exists():
        raise BillingError("staff_required")
    amount = _minor(amount_minor)
    if amount == 0:
        raise BillingError("invalid_amount")
    for value in (provider, merchant_account, external_payment_id, evidence_ref):
        if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
            raise BillingError("invalid_payment_reference")
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise BillingError("invalid_currency")
    reference = (
        Invoice.objects.filter(pk=invoice_id, period__subscription__account__mode=_mode())
        .values("period_id", "period__subscription__account_id")
        .first()
    )
    if reference is None:
        raise BillingError("not_found")
    account = BillingAccount.objects.select_for_update().get(
        pk=reference["period__subscription__account_id"]
    )
    period = BillingPeriod.objects.select_for_update().get(pk=reference["period_id"])
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    existing = PaymentReceipt.objects.filter(
        provider=provider,
        merchant_account=merchant_account,
        mode=account.mode,
        external_payment_id=external_payment_id,
    ).first()
    if existing is not None:
        if (
            existing.invoice_id != invoice.pk
            or existing.amount_minor != amount
            or existing.currency != currency
            or existing.evidence_ref != evidence_ref
        ):
            raise BillingError("payment_conflict")
        return existing
    if invoice.status != "open":
        raise BillingError("invoice_not_open")
    if invoice.amount_minor != amount or invoice.currency != currency:
        raise BillingError("payment_mismatch")
    try:
        # A cross-account duplicate can race before either insert commits.
        with transaction.atomic():
            receipt = PaymentReceipt.objects.create(
                invoice=invoice,
                provider=provider,
                merchant_account=merchant_account,
                mode=account.mode,
                external_payment_id=external_payment_id,
                amount_minor=amount,
                currency=currency,
                evidence_ref=evidence_ref,
                recorded_by_id=actor_id,
            )
    except (IntegrityError, ValidationError):
        raise BillingError("payment_conflict") from None
    paid_at = _now()
    invoice.status = "paid"
    invoice.settlement_kind = "payment"
    invoice.paid_at = paid_at
    invoice.save(update_fields=["status", "settlement_kind", "paid_at"])
    if invoice.kind == "base":
        if period.paid_at is None:
            period.paid_at = paid_at
            period.save(update_fields=["paid_at"])
        subscription = Subscription.objects.get(pk=period.subscription_id)
        # Payment retries never reactivate cancellation; they returned above.
        if subscription.status == "pending" and period.ends_at > paid_at:
            subscription.status = "active"
            subscription.save(update_fields=["status"])
    return receipt


@transaction.atomic
def issue_overage(*, owner_id: int, period_id: UUID) -> Invoice:
    """Close an ended, fully settled period; bill no more than its consent cap."""
    from src.usage.models import LLMInvocation

    account = _account(owner_id)
    period = (
        BillingPeriod.objects.select_for_update()
        .filter(pk=period_id, subscription__account=account)
        .first()
    )
    if period is None:
        raise BillingError("not_found")
    existing = Invoice.objects.filter(period=period, kind="overage").first()
    if existing is not None:
        return existing
    now = _now()
    if period.paid_at is None:
        raise BillingError("unpaid_period")
    if period.ends_at > now:
        raise BillingError("period_not_ended")
    if (
        period.reserved_tokens
        or period.reserved_calls
        or LLMInvocation.objects.filter(billing_period=period, reservation_active=True).exists()
    ):
        raise BillingError("usage_unsettled")
    raw = overage_minor(period)
    amount = min(raw, period.overage_limit_minor)
    invoice = Invoice.objects.create(
        period=period,
        kind="overage",
        amount_minor=amount,
        raw_overage_minor=raw,
        currency=period.plan.currency,
        status="paid" if amount == 0 else "open",
        settlement_kind="zero_balance" if amount == 0 else "",
        paid_at=now if amount == 0 else None,
    )
    period.closed_at = now
    period.save(update_fields=["closed_at"])
    return invoice
