"""Synchronous entitlement helpers used inside the usage transaction.

Run locks precede these account/period locks. Provider I/O never belongs in this
module, and period identity remains frozen on every admitted invocation.
"""

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import BillingAccount, BillingPeriod, Invoice, Subscription

_MILLION = 1_000_000


def overage_minor(period, *, tokens=None, calls=None) -> int:
    """Price cumulative usage once, in integer minor units, rounding upward.

    Defaults are settled actual usage, excluding outstanding reservations. An
    admission must pass projected actual + reserved + new totals explicitly.
    Zero tariff rates permit free overage; OwnerQuota remains a separate hard cap.
    """
    tokens = period.used_tokens if tokens is None else tokens
    calls = period.used_calls if calls is None else calls
    values = (
        tokens,
        calls,
        period.plan.included_tokens,
        period.plan.included_requests,
        period.plan.overage_per_million_tokens_minor,
        period.plan.overage_per_request_minor,
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Billing usage and tariffs must be nonnegative integers")
    numerator = (
        max(0, tokens - period.plan.included_tokens) * period.plan.overage_per_million_tokens_minor
        + max(0, calls - period.plan.included_requests)
        * period.plan.overage_per_request_minor
        * _MILLION
    )
    return (numerator + _MILLION - 1) // _MILLION


def lock_current_period(owner_id):
    """Return the locked paid period and a bounded denial code, if any.

    All subscription changes serialize on the account. Billing disabled affects
    new admissions only; settlement must still lock an invocation's old period.
    """
    if not settings.BILLING_ENABLED:
        return None, None
    if owner_id is None:
        return None, "billing_owner_required"
    account = (
        BillingAccount.objects.select_for_update()
        .filter(owner_id=owner_id, mode=settings.BILLING_MODE)
        .first()
    )
    if account is None:
        return None, "billing_account_required"
    if account.blocked:
        return None, "billing_account_blocked"
    # Waiting for the account lock can cross a period boundary. The eligibility
    # clock must be read after serialization, rather than before that wait.
    now = timezone.now()
    if has_outstanding_debt(account, now):
        return None, "billing_outstanding_debt"
    subscription = Subscription.objects.filter(account=account, status="active").first()
    if subscription is None:
        return None, "billing_subscription_inactive"
    period = (
        BillingPeriod.objects.select_for_update(of=("self",))
        .select_related("plan")
        .filter(
            subscription=subscription,
            paid_at__isnull=False,
            closed_at__isnull=True,
            starts_at__lte=now,
            ends_at__gt=now,
        )
        .order_by("-starts_at", "pk")
        .first()
    )
    if period is None:
        return None, "billing_paid_period_required"
    return period, None


def has_outstanding_debt(account, now) -> bool:
    """Check ended paid periods while the caller holds the account lock.

    Positive authorized overage requires reconciliation/collection before any
    next-period admission. A zero-cap period cannot create collectible debt.
    Query active invocations too: a zero-token estimate can leave an unresolved
    dispatched call even when both reservation counters happen to be zero.
    """
    periods = BillingPeriod.objects.filter(
        subscription__account=account,
        paid_at__isnull=False,
        ends_at__lte=now,
        overage_limit_minor__gt=0,
    )
    if periods.filter(invocations__reservation_active=True).exists():
        return True
    paid_invoice = Invoice.objects.filter(period_id=OuterRef("pk"), kind="overage", status="paid")
    unpaid = periods.annotate(overage_paid=Exists(paid_invoice)).filter(overage_paid=False)
    for period in unpaid.select_related("plan").iterator(chunk_size=200):
        if min(overage_minor(period), period.overage_limit_minor) > 0:
            return True
    return False


def lock_invocation_period(period_id):
    """Lock the original account/period independently of feature flag or mode."""
    if period_id is None:
        return None
    account_id = BillingPeriod.objects.values_list("subscription__account_id", flat=True).get(
        pk=period_id
    )
    BillingAccount.objects.select_for_update().get(pk=account_id)
    return (
        BillingPeriod.objects.select_for_update(of=("self",))
        .select_related("plan")
        .get(pk=period_id)
    )


def reserve_period(period, estimated_tokens):
    """Apply an already-authorized reservation while holding the period lock."""
    if period is not None:
        period.reserved_tokens += estimated_tokens
        period.reserved_calls += 1
        period.save(update_fields=["reserved_tokens", "reserved_calls"])


def period_denial(period, estimated_tokens):
    if (
        period is not None
        and overage_minor(
            period,
            tokens=period.used_tokens + period.reserved_tokens + estimated_tokens,
            calls=period.used_calls + period.reserved_calls + 1,
        )
        > period.overage_limit_minor
    ):
        return "billing_overage_limit"
    return None
