"""Bounded period closing skips unresolved usage and never invents a payment."""

import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from src.billing import services
from src.billing.management.commands import billing_close_periods
from src.billing.models import (
    BillingAccount,
    BillingPeriod,
    Invoice,
    PaymentReceipt,
    PlanVersion,
    Subscription,
)
from src.runs.models import WorkflowRun
from src.usage.models import LLMInvocation
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


@pytest.fixture
def periods(settings, monkeypatch):
    settings.BILLING_MODE = "test"
    settings.BILLING_ENABLED = False
    monkeypatch.setattr(billing_close_periods.timezone, "now", lambda: NOW)
    monkeypatch.setattr(services, "_now", lambda: NOW)
    plan = PlanVersion.objects.create(
        code="operations",
        version="v1",
        name="Monthly operations fixture",
        price_minor=1000,
        currency="USD",
        included_tokens=100,
        included_requests=10,
        overage_per_million_tokens_minor=1_000_000,
        overage_per_request_minor=2,
    )

    def create(*, mode="test", ends_at=NOW - timedelta(hours=1), cap=100, **values):
        owner = get_user_model().objects.create_user(username="closing-" + uuid.uuid4().hex)
        account = BillingAccount.objects.create(owner=owner, mode=mode)
        subscription = Subscription.objects.create(
            account=account,
            plan=plan,
            status="active",
            anchor_day=1,
            next_overage_limit_minor=cap,
        )
        # This fixture stands for an already funded base period. The command
        # only settles its overage; importing a real payment is tested separately.
        return BillingPeriod.objects.create(
            subscription=subscription,
            plan=plan,
            starts_at=ends_at - timedelta(days=30),
            ends_at=ends_at,
            paid_at=ends_at - timedelta(days=29),
            overage_limit_minor=cap,
            **values,
        )

    return create


def close(**options):
    output = StringIO()
    call_command("billing_close_periods", stdout=output, **options)
    return output.getvalue()


def test_dry_run_reports_eligible_periods_without_creating_financial_records(periods):
    period = periods()
    output = close(dry_run=True)
    period.refresh_from_db()
    assert "Eligible candidates: 1" in output
    assert period.closed_at is None
    assert not Invoice.objects.exists()
    assert not PaymentReceipt.objects.exists()


def test_repeated_closing_settles_zero_balance_once_without_a_payment_receipt(periods):
    period = periods(used_tokens=100, used_calls=10)
    assert "Issued: 1; deferred: 0" in close()
    invoice = Invoice.objects.get(period=period, kind="overage")
    period.refresh_from_db()
    assert invoice.amount_minor == invoice.raw_overage_minor == 0
    assert invoice.status == "paid" and invoice.settlement_kind == "zero_balance"
    assert invoice.paid_at == period.closed_at == NOW
    assert not PaymentReceipt.objects.exists()
    assert "Issued: 0; deferred: 0" in close()
    assert Invoice.objects.get().pk == invoice.pk


def test_closing_issues_an_exact_capped_positive_invoice_without_collecting_payment(periods):
    period = periods(cap=20, used_tokens=150, used_calls=12)
    assert "Issued: 1; deferred: 0" in close()
    invoice = Invoice.objects.get(period=period, kind="overage")
    assert invoice.raw_overage_minor == 54
    assert invoice.amount_minor == 20 and invoice.currency == "USD"
    assert invoice.status == "open" and invoice.settlement_kind == ""
    assert invoice.paid_at is None
    assert not PaymentReceipt.objects.exists()


def test_cleanup_obeys_mode_partition_while_billing_admission_is_disabled(periods, settings):
    test_period = periods(mode="test")
    live_period = periods(mode="live")
    assert not settings.BILLING_ENABLED
    assert "Issued: 1; deferred: 0" in close()
    test_period.refresh_from_db()
    live_period.refresh_from_db()
    assert test_period.closed_at == NOW and live_period.closed_at is None
    assert set(Invoice.objects.values_list("period_id", flat=True)) == {test_period.pk}
    settings.BILLING_MODE = "live"
    assert "Issued: 1; deferred: 0" in close()
    live_period.refresh_from_db()
    assert live_period.closed_at == NOW
    assert not PaymentReceipt.objects.exists()


@pytest.mark.parametrize("hold", ["zero_estimate", "reserved_tokens", "reserved_calls"])
def test_old_unknown_holds_cannot_starve_a_ready_period_with_limit_one(periods, hold):
    values = {hold: 1} if hold != "zero_estimate" else {}
    old = periods(ends_at=NOW - timedelta(days=3), **values)
    ready = periods(ends_at=NOW - timedelta(days=1))
    if hold == "zero_estimate":
        owner = old.subscription.account.owner
        workflow = Workflow.objects.create(
            owner=owner,
            name="Unknown transport outcome",
            nodes=[
                {
                    "id": "start",
                    "kind": "condition",
                    "config": {"condition": {"path": "enabled", "op": "exists"}},
                }
            ],
            edges=[],
        )
        run = WorkflowRun.objects.create(workflow=workflow, billing_owner=owner, status="failed")
        LLMInvocation.objects.create(
            owner=owner,
            run=run,
            billing_period=old,
            node_id="unknown",
            logical_call=1,
            attempt=1,
            model_name="fixture",
            provider="openai",
            operation="complete",
            status="unknown",
            reserved_tokens=0,
            max_output_tokens=0,
            reservation_active=True,
            dispatched_at=old.ends_at - timedelta(seconds=1),
            finished_at=old.ends_at,
        )
    assert "Issued: 1; deferred: 0" in close(limit=1)
    old.refresh_from_db()
    ready.refresh_from_db()
    assert old.closed_at is None
    assert ready.closed_at == NOW
    assert not Invoice.objects.filter(period=old, kind="overage").exists()
    assert Invoice.objects.get(period=ready, kind="overage").settlement_kind == "zero_balance"
    assert "Issued: 0; deferred: 0" in close(limit=1)
