"""Financial records require explicit evidence and retain their original terms."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections

from src.billing import services
from src.billing.contracts import MAX_MINOR, BillingError
from src.billing.models import (
    BillingAccount,
    BillingPeriod,
    Invoice,
    PaymentReceipt,
    PlanVersion,
    Subscription,
)

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 1, 31, 12, 30, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    monkeypatch.setattr(services, "_now", lambda: current[0])
    return current


@pytest.fixture
def billing(settings, clock):
    settings.BILLING_ENABLED = True
    settings.BILLING_MODE = "test"
    owner = get_user_model().objects.create_user(username="billing-domain-owner")
    staff = get_user_model().objects.create_user(username="billing-domain-staff", is_staff=True)
    plan = PlanVersion.objects.create(
        code="standard",
        version="v1",
        name="Configured monthly plan",
        price_minor=1500,
        currency="USD",
        included_tokens=100,
        included_requests=2,
        overage_per_million_tokens_minor=100_000,
        overage_per_request_minor=3,
    )
    return owner, staff, plan


def start(billing, *, cap=100):
    owner, _, plan = billing
    return services.start_subscription(owner_id=owner.pk, plan_id=plan.pk, overage_limit_minor=cap)


def pay(billing, invoice, **changes):
    _, staff, _ = billing
    values = {
        "invoice_id": invoice.pk,
        "actor_id": staff.pk,
        "provider": "manual",
        "merchant_account": "operator-bank-account",
        "external_payment_id": "payment-" + str(invoice.pk),
        "amount_minor": invoice.amount_minor,
        "currency": invoice.currency,
        "evidence_ref": "statement:" + str(invoice.pk),
    }
    values.update(changes)
    return services.record_payment(**values)


def funded(billing, *, cap=100):
    period = start(billing, cap=cap)
    pay(billing, Invoice.objects.get(period=period, kind="base"))
    period.refresh_from_db()
    return period


def assert_error(code, function, **kwargs):
    with pytest.raises(BillingError) as error:
        function(**kwargs)
    assert error.value.code == code
    return error.value


def test_start_is_an_unpaid_request_and_retries_do_not_duplicate_it(billing):
    period = start(billing)
    again = start(billing)
    assert again.pk == period.pk
    assert period.starts_at == NOW
    assert period.ends_at == datetime(2026, 2, 28, 12, 30, tzinfo=UTC)
    assert period.paid_at is None
    assert period.overage_limit_minor == 100
    assert period.subscription.status == "pending"
    invoice = Invoice.objects.get(period=period)
    assert (invoice.kind, invoice.status, invoice.amount_minor, invoice.currency) == (
        "base",
        "open",
        1500,
        "USD",
    )
    assert BillingAccount.objects.count() == Subscription.objects.count() == 1
    assert BillingPeriod.objects.count() == Invoice.objects.count() == 1
    assert not PaymentReceipt.objects.exists()


@pytest.mark.parametrize("cap", [-1, True, MAX_MINOR + 1, 0.5])
def test_invalid_consent_cannot_create_financial_records(billing, cap):
    assert_error("invalid_amount", start, billing=billing, cap=cap)
    assert not BillingAccount.objects.exists()


def test_invalid_or_disabled_plan_rolls_back_new_account(billing):
    owner, _, plan = billing
    for plan_id in (uuid.uuid4(), plan.pk):
        plan.enabled = False
        plan.save(update_fields=["enabled"])
        assert_error(
            "invalid_plan", services.start_subscription, owner_id=owner.pk, plan_id=plan_id
        )
    assert not BillingAccount.objects.exists()


def test_pending_subscription_does_not_accept_changed_terms_by_retry(billing):
    start(billing, cap=10)
    assert_error("subscription_conflict", start, billing=billing, cap=11)
    assert Invoice.objects.count() == BillingPeriod.objects.count() == 1


def test_recording_exact_evidence_is_idempotent_and_activates_only_base_period(billing):
    period = start(billing)
    invoice = Invoice.objects.get(period=period)
    first = pay(billing, invoice)
    duplicate = pay(billing, invoice)
    period.refresh_from_db()
    invoice.refresh_from_db()
    period.subscription.refresh_from_db()
    assert duplicate.pk == first.pk
    assert PaymentReceipt.objects.count() == 1
    assert period.paid_at == invoice.paid_at == NOW
    assert period.subscription.status == "active"
    assert invoice.status == "paid" and invoice.settlement_kind == "payment"
    assert (first.mode, first.recorded_by_id, first.amount_minor) == (
        "test",
        billing[1].pk,
        1500,
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"amount_minor": 1499}, "payment_mismatch"),
        ({"currency": "EUR"}, "payment_mismatch"),
        ({"currency": "usd"}, "invalid_currency"),
        ({"external_payment_id": "secret\nvalue"}, "invalid_payment_reference"),
        ({"amount_minor": True}, "invalid_amount"),
    ],
)
def test_payment_evidence_must_match_frozen_invoice_exactly(billing, changes, code):
    period = start(billing)
    invoice = Invoice.objects.get(period=period)
    assert_error(code, pay, billing=billing, invoice=invoice, **changes)
    invoice.refresh_from_db()
    period.refresh_from_db()
    assert invoice.status == "open" and period.paid_at is None
    assert not PaymentReceipt.objects.exists()


def test_service_rechecks_active_staff_instead_of_trusting_its_caller(billing):
    invoice = Invoice.objects.get(period=start(billing))
    assert_error("staff_required", pay, billing=billing, invoice=invoice, actor_id=billing[0].pk)
    staff = billing[1]
    staff.is_active = False
    staff.save(update_fields=["is_active"])
    assert_error("staff_required", pay, billing=billing, invoice=invoice)
    assert not PaymentReceipt.objects.exists()


def test_payment_namespace_cannot_be_rebound_to_another_invoice(billing):
    period = start(billing)
    invoice = Invoice.objects.get(period=period)
    receipt = pay(billing, invoice, external_payment_id="same-external-payment")
    next_period = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    next_invoice = Invoice.objects.get(period=next_period)
    assert_error(
        "payment_conflict",
        pay,
        billing=billing,
        invoice=next_invoice,
        external_payment_id="same-external-payment",
    )
    next_invoice.refresh_from_db()
    assert next_invoice.status == "open"
    assert PaymentReceipt.objects.get().pk == receipt.pk


def test_replay_with_changed_evidence_is_rejected_without_overwriting_history(billing):
    invoice = Invoice.objects.get(period=start(billing))
    receipt = pay(billing, invoice)
    assert_error(
        "payment_conflict", pay, billing=billing, invoice=invoice, evidence_ref="changed-reference"
    )
    assert PaymentReceipt.objects.get().evidence_ref == receipt.evidence_ref


def test_test_and_live_accounts_are_separate_and_do_not_share_evidence(billing, settings):
    test_period = start(billing)
    test_invoice = Invoice.objects.get(period=test_period)
    pay(billing, test_invoice, external_payment_id="provider-id")
    settings.BILLING_MODE = "live"
    assert_error("not_found", pay, billing=billing, invoice=test_invoice)
    live_period = start(billing)
    live_invoice = Invoice.objects.get(period=live_period)
    pay(billing, live_invoice, external_payment_id="provider-id")
    assert BillingAccount.objects.count() == 2
    assert set(PaymentReceipt.objects.values_list("mode", flat=True)) == {"test", "live"}


def test_calendar_renewal_does_not_drift_after_short_february(billing, clock):
    period = funded(billing)
    second = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    assert second.starts_at == period.ends_at
    assert second.ends_at == datetime(2026, 3, 31, 12, 30, tzinfo=UTC)
    assert (
        services.renew_subscription(
            owner_id=billing[0].pk, subscription_id=period.subscription_id
        ).pk
        == second.pk
    )
    pay(billing, Invoice.objects.get(period=second))
    assert (
        services.renew_subscription(
            owner_id=billing[0].pk, subscription_id=period.subscription_id
        ).pk
        == second.pk
    )
    clock[0] = second.starts_at
    third = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    assert third.starts_at == second.ends_at
    assert third.ends_at == datetime(2026, 4, 30, 12, 30, tzinfo=UTC)


def test_next_cap_change_does_not_rewrite_created_or_paid_periods(billing):
    period = funded(billing, cap=10)
    services.set_next_overage_limit(
        owner_id=billing[0].pk, subscription_id=period.subscription_id, overage_limit_minor=50
    )
    following = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    services.set_next_overage_limit(
        owner_id=billing[0].pk, subscription_id=period.subscription_id, overage_limit_minor=80
    )
    period.refresh_from_db()
    following.refresh_from_db()
    assert period.overage_limit_minor == 10
    assert following.overage_limit_minor == 50
    assert following.subscription.next_overage_limit_minor == 80


def test_cancel_honors_paid_time_and_voids_unpaid_future_invoices(billing, clock):
    period = funded(billing)
    next_period = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    next_invoice = Invoice.objects.get(period=next_period)
    subscription = services.cancel_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    assert subscription.status == "active" and subscription.cancel_at_period_end
    next_invoice.refresh_from_db()
    assert next_invoice.status == "void"
    assert_error("invoice_not_open", pay, billing=billing, invoice=next_invoice)
    assert_error(
        "subscription_canceled",
        services.renew_subscription,
        owner_id=billing[0].pk,
        subscription_id=subscription.pk,
    )
    # An ended scheduled cancellation can begin an explicit new lifecycle.
    clock[0] = period.ends_at + timedelta(days=2)
    restarted = start(billing, cap=15)
    assert restarted.starts_at == clock[0]
    assert restarted.subscription_id == subscription.pk
    assert restarted.subscription.status == "pending"
    assert not restarted.subscription.cancel_at_period_end
    assert restarted.overage_limit_minor == 15


def test_payment_retry_does_not_reverse_cancellation_or_shift_period_dates(billing):
    period = funded(billing)
    invoice = Invoice.objects.get(period=period)
    services.cancel_subscription(owner_id=billing[0].pk, subscription_id=period.subscription_id)
    pay(billing, invoice)
    period.refresh_from_db()
    period.subscription.refresh_from_db()
    assert period.subscription.cancel_at_period_end
    assert period.starts_at == NOW
    assert period.ends_at == datetime(2026, 2, 28, 12, 30, tzinfo=UTC)


def test_restart_at_a_void_future_period_boundary_is_a_safe_conflict(billing, clock):
    period = funded(billing, cap=0)
    future = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    services.cancel_subscription(owner_id=billing[0].pk, subscription_id=period.subscription_id)
    clock[0] = future.starts_at
    assert_error("period_conflict", start, billing=billing, cap=0)
    assert BillingPeriod.objects.count() == 2
    period.subscription.refresh_from_db()
    assert period.subscription.cancel_at_period_end
    assert Invoice.objects.get(period=future).status == "void"


@pytest.mark.parametrize("paid", [False, True])
def test_renewal_after_an_unpaid_gap_starts_now_with_a_new_anchor(billing, clock, paid):
    period = funded(billing, cap=0) if paid else start(billing, cap=0)
    invoice = Invoice.objects.get(period=period)
    clock[0] = datetime(2026, 5, 2, 9, tzinfo=UTC)
    renewal = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    assert renewal.starts_at == clock[0]
    assert renewal.ends_at == datetime(2026, 6, 2, 9, tzinfo=UTC)
    invoice.refresh_from_db()
    assert invoice.status == ("paid" if paid else "void")


def test_expired_payment_is_history_and_does_not_extend_the_period(billing, clock):
    period = start(billing)
    clock[0] = period.ends_at + timedelta(days=5)
    pay(billing, Invoice.objects.get(period=period))
    period.refresh_from_db()
    assert period.paid_at == clock[0]
    assert period.ends_at < clock[0]
    assert period.subscription.status == "pending"


def test_overage_prices_cumulative_actual_once_and_never_exceeds_consent(billing, clock):
    period = funded(billing, cap=4)
    # 35 excess tokens * 0.1 minor + 1 excess call * 3 minor = 6.5 -> 7 minor.
    period.used_tokens = 135
    period.used_calls = 3
    period.save(update_fields=["used_tokens", "used_calls"])
    clock[0] = period.ends_at
    invoice = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    duplicate = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    assert duplicate.pk == invoice.pk
    assert (invoice.kind, invoice.amount_minor, invoice.raw_overage_minor, invoice.status) == (
        "overage",
        4,
        7,
        "open",
    )
    period.refresh_from_db()
    assert period.closed_at == clock[0]
    pay(billing, invoice)
    invoice.refresh_from_db()
    assert invoice.status == "paid"
    assert PaymentReceipt.objects.count() == 2


def test_zero_balance_closes_without_a_fictitious_payment_receipt(billing, clock):
    period = funded(billing)
    clock[0] = period.ends_at
    invoice = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    assert invoice.amount_minor == invoice.raw_overage_minor == 0
    assert invoice.status == "paid"
    assert invoice.settlement_kind == "zero_balance"
    assert invoice.paid_at == clock[0]
    assert not PaymentReceipt.objects.filter(invoice=invoice).exists()


def test_actual_overage_beyond_bigint_is_retained_while_the_invoice_stays_capped(billing, clock):
    period = funded(billing, cap=4)
    period.used_tokens = period.used_calls = 2**63 - 1
    period.save(update_fields=["used_tokens", "used_calls"])
    clock[0] = period.ends_at
    invoice = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    invoice.refresh_from_db()
    assert invoice.raw_overage_minor > 2**63 - 1
    assert invoice.amount_minor == 4


def test_historical_financial_terms_cannot_be_repriced_by_later_model_writes(billing, clock):
    period = funded(billing, cap=7)
    base_invoice = Invoice.objects.get(period=period, kind="base")
    plan = billing[2]
    for field, changed in (
        ("price_minor", 9000),
        ("currency", "EUR"),
        ("overage_per_million_tokens_minor", 900_000),
    ):
        setattr(plan, field, changed)
        with pytest.raises(ValidationError, match="immutable"):
            plan.save()
        plan.refresh_from_db()
    for field, changed in (
        ("overage_limit_minor", 100),
        ("ends_at", period.ends_at + timedelta(days=10)),
    ):
        setattr(period, field, changed)
        with pytest.raises(ValidationError, match="immutable"):
            period.save()
        period.refresh_from_db()
    period.used_tokens, period.used_calls = 120, 3
    period.save(update_fields=["used_tokens", "used_calls"])
    clock[0] = period.ends_at
    overage = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    base_invoice.refresh_from_db()
    assert (base_invoice.amount_minor, base_invoice.currency) == (1500, "USD")
    assert (overage.raw_overage_minor, overage.amount_minor, overage.currency) == (5, 5, "USD")
    assert period.overage_limit_minor == 7


def test_period_cannot_close_before_end_or_while_usage_is_held(billing, clock):
    period = funded(billing)
    assert_error(
        "period_not_ended", services.issue_overage, owner_id=billing[0].pk, period_id=period.pk
    )
    clock[0] = period.ends_at
    period.reserved_tokens = 10
    period.save(update_fields=["reserved_tokens"])
    assert_error(
        "usage_unsettled", services.issue_overage, owner_id=billing[0].pk, period_id=period.pk
    )
    assert not Invoice.objects.filter(period=period, kind="overage").exists()


def test_zero_estimate_unknown_invocation_also_prevents_closing(billing, clock):
    from src.runs.models import WorkflowRun
    from src.usage.models import LLMInvocation
    from src.workflows.models import Workflow

    period = funded(billing)
    workflow = Workflow.objects.create(
        owner=billing[0],
        name="Unknown billing usage",
        nodes=[{"id": "wait", "kind": "hitl_approval"}],
    )
    run = WorkflowRun.objects.create(workflow=workflow, billing_owner=billing[0], status="running")
    LLMInvocation.objects.create(
        run=run,
        owner=billing[0],
        billing_period=period,
        node_id="call",
        logical_call=1,
        attempt=1,
        model_name="mock-model",
        provider="openai_compatible",
        operation="chat",
        status="dispatched",
        reserved_tokens=0,
        max_output_tokens=0,
        dispatched_at=NOW,
    )
    clock[0] = period.ends_at
    assert period.reserved_tokens == period.reserved_calls == 0
    assert_error(
        "usage_unsettled", services.issue_overage, owner_id=billing[0].pk, period_id=period.pk
    )


def test_ended_overage_debt_blocks_new_period_until_exact_payment(billing, clock):
    period = funded(billing, cap=100)
    period.used_tokens = 120
    period.save(update_fields=["used_tokens"])
    clock[0] = period.ends_at
    assert_error(
        "outstanding_overage",
        services.renew_subscription,
        owner_id=billing[0].pk,
        subscription_id=period.subscription_id,
    )
    debt = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    assert debt.amount_minor == 2
    pay(billing, debt)
    renewed = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    assert renewed.starts_at == period.ends_at


def test_existing_future_base_payment_is_recorded_even_when_prior_overage_is_owed(billing, clock):
    from src.billing.entitlements import has_outstanding_debt

    period = funded(billing, cap=100)
    future = services.renew_subscription(
        owner_id=billing[0].pk, subscription_id=period.subscription_id
    )
    period.used_tokens = 120
    period.save(update_fields=["used_tokens"])
    clock[0] = period.ends_at
    # This attests received money; the debt gate remains separate from that fact.
    receipt = pay(billing, Invoice.objects.get(period=future, kind="base"))
    future.refresh_from_db()
    assert future.paid_at == NOW.replace(month=2, day=28)
    assert receipt.invoice.period_id == future.pk
    assert has_outstanding_debt(period.subscription.account, clock[0])


def test_cleanup_remains_available_when_billing_is_disabled(billing, clock, settings):
    period = start(billing)
    settings.BILLING_ENABLED = False
    pay(billing, Invoice.objects.get(period=period))
    services.set_next_overage_limit(
        owner_id=billing[0].pk, subscription_id=period.subscription_id, overage_limit_minor=7
    )
    services.cancel_subscription(owner_id=billing[0].pk, subscription_id=period.subscription_id)
    clock[0] = period.ends_at
    invoice = services.issue_overage(owner_id=billing[0].pk, period_id=period.pk)
    assert invoice.status == "paid"
    assert_error("billing_disabled", start, billing=billing)
    assert_error(
        "billing_disabled",
        services.renew_subscription,
        owner_id=billing[0].pk,
        subscription_id=period.subscription_id,
    )


@pytest.mark.postgresql
@pytest.mark.parametrize("operation", ["start", "payment", "close"])
def test_concurrent_retries_create_one_financial_effect(billing, clock, operation):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    if operation == "start":

        def action():
            return start(billing).pk

        model = BillingPeriod
    elif operation == "payment":
        invoice = Invoice.objects.get(period=start(billing))

        def action():
            return pay(billing, invoice).pk

        model = PaymentReceipt
    else:
        period = funded(billing)
        clock[0] = period.ends_at

        def action():
            return services.issue_overage(owner_id=billing[0].pk, period_id=period.pk).pk

        model = Invoice
    barrier = Barrier(2)

    def concurrent():
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            return action()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: concurrent(), range(2)))
    assert results[0] == results[1]
    assert model.objects.count() == (2 if operation == "close" else 1)


@pytest.mark.postgresql
def test_concurrent_cross_account_payment_reuse_has_one_winner(billing):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL unique evidence arbitration required")
    invoice = Invoice.objects.get(period=start(billing))
    another = get_user_model().objects.create_user(username="other-billing-domain-owner")
    second_period = services.start_subscription(owner_id=another.pk, plan_id=billing[2].pk)
    second_invoice = Invoice.objects.get(period=second_period)
    barrier = Barrier(2)

    def concurrent(invoice_id):
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            return pay(
                billing,
                Invoice.objects.get(pk=invoice_id),
                external_payment_id="cross-account-external-id",
                evidence_ref="same-statement",
            ).pk
        except BillingError as error:
            return error.code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(concurrent, (invoice.pk, second_invoice.pk)))
    assert results.count("payment_conflict") == 1
    assert PaymentReceipt.objects.count() == 1
    assert Invoice.objects.filter(status="paid").count() == 1
