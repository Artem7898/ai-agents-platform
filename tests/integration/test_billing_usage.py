"""Paid entitlements compose atomically with existing per-run and owner quotas."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
import pytest_asyncio
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.utils import timezone

from src.billing.entitlements import overage_minor
from src.billing.models import BillingAccount, BillingPeriod, Invoice, PlanVersion, Subscription
from src.runs.models import RunEvent, WorkflowRun
from src.usage import services
from src.usage.contracts import InvocationContext, QuotaExceeded
from src.usage.models import LLMInvocation, OwnerQuota, QuotaWindow
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]
USAGE = {"prompt_tokens": 4, "completion_tokens": 8, "total_tokens": 12}


@pytest_asyncio.fixture
async def billing(settings):
    settings.BILLING_ENABLED = True
    settings.BILLING_MODE = "test"
    now = timezone.now()
    owner = await get_user_model().objects.acreate(username=f"paid-{uuid.uuid4()}")
    plan = await PlanVersion.objects.acreate(
        code="metered",
        version="v1",
        name="Metered",
        price_minor=100,
        currency="USD",
        included_tokens=100,
        included_requests=10,
        overage_per_million_tokens_minor=1_000_000,
        overage_per_request_minor=5,
    )
    account = await BillingAccount.objects.acreate(owner=owner, mode="test")
    subscription = await Subscription.objects.acreate(
        account=account,
        plan=plan,
        status="active",
        anchor_day=now.day,
        next_overage_limit_minor=10,
    )
    period = await BillingPeriod.objects.acreate(
        subscription=subscription,
        plan=plan,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=29),
        paid_at=now,
        overage_limit_minor=10,
    )
    workflow = await Workflow.objects.acreate(
        owner=owner,
        name="Paid workflow",
        nodes=[{"id": "start", "kind": "hitl_approval"}],
        edges=[],
    )
    run = await WorkflowRun.objects.acreate(
        workflow=workflow,
        billing_owner=owner,
        status="running",
        workflow_snapshot={"limits": {"max_tokens": 10_000}},
    )
    quota = await OwnerQuota.objects.acreate(owner=owner, token_limit=5_000, request_limit=100)
    return SimpleNamespace(
        owner=owner,
        plan=plan,
        account=account,
        subscription=subscription,
        period=period,
        run=run,
        quota=quota,
    )


async def admit(run, *, estimate=20):
    return await services.reserve(
        InvocationContext(run.pk, "answer", 1),
        model_name="paid-model",
        provider="openai",
        estimated_tokens=estimate,
        max_output_tokens=10,
        operation="chat",
    )


async def buckets(billing):
    await billing.period.arefresh_from_db()
    window = await QuotaWindow.objects.aget(quota=billing.quota)
    return billing.period, window


@pytest.mark.parametrize(
    "tokens,calls,expected",
    [
        (0, 0, 0),
        (1, 0, 1),
        (999_999, 0, 1),
        (1_000_000, 0, 1),
        (1_000_001, 0, 2),
        (1, 1, 3),
        (1_000_001, 2, 6),
    ],
)
async def test_overage_rounds_cumulative_combined_usage_once(tokens, calls, expected):
    plan = SimpleNamespace(
        included_tokens=0,
        included_requests=0,
        overage_per_million_tokens_minor=1,
        overage_per_request_minor=2,
    )
    period = SimpleNamespace(plan=plan, used_tokens=tokens, used_calls=calls)
    assert overage_minor(period) == expected
    assert overage_minor(period, tokens=0, calls=0) == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
async def test_overage_rejects_non_integer_or_negative_usage(value):
    plan = SimpleNamespace(
        included_tokens=0,
        included_requests=0,
        overage_per_million_tokens_minor=1,
        overage_per_request_minor=1,
    )
    with pytest.raises(ValueError):
        overage_minor(SimpleNamespace(plan=plan, used_tokens=0, used_calls=0), tokens=value)


async def test_paid_admission_and_idempotent_settlement_update_both_layers(billing):
    reservation = await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=reservation.id)
    assert row.billing_period_id == billing.period.pk
    for bucket in await buckets(billing):
        assert (bucket.reserved_tokens, bucket.reserved_calls, bucket.used_calls) == (20, 1, 0)
    await services.mark_dispatched(row.pk)
    await services.mark_dispatched(row.pk)
    await services.finalize(row.pk, status="succeeded", usage=USAGE)
    await services.finalize(row.pk, status="succeeded", usage=USAGE)
    for bucket in await buckets(billing):
        assert (bucket.used_tokens, bucket.used_calls) == (12, 1)
        assert (bucket.reserved_tokens, bucket.reserved_calls) == (0, 0)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "unknown"])
async def test_dispatched_unknown_retains_both_token_holds(billing, status):
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status=status)
    row = await LLMInvocation.objects.aget(pk=reservation.id)
    assert row.reservation_active and row.total_tokens is None
    for bucket in await buckets(billing):
        assert (bucket.reserved_tokens, bucket.reserved_calls, bucket.used_calls) == (20, 0, 1)


async def test_unsent_and_reconciled_not_sent_release_both_layers_exactly_once(billing):
    unsent = await admit(billing.run)
    await services.finalize(unsent.id, status="failed")
    sent = await admit(billing.run)
    await services.mark_dispatched(sent.id)
    await services.finalize(sent.id, status="unknown")
    await services.reconcile(sent.id, not_sent=True)
    await services.reconcile(sent.id, not_sent=True)
    for bucket in await buckets(billing):
        assert (bucket.reserved_tokens, bucket.reserved_calls) == (0, 0)
        assert (bucket.used_tokens, bucket.used_calls) == (0, 0)


async def test_partial_then_known_usage_settles_once_after_billing_mode_changes(billing, settings):
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="succeeded", usage={"prompt_tokens": 4})
    settings.BILLING_ENABLED = False
    settings.BILLING_MODE = "live"
    await services.finalize(reservation.id, status="succeeded", usage=USAGE)
    await services.finalize(reservation.id, status="succeeded", usage=USAGE)
    for bucket in await buckets(billing):
        assert (bucket.used_tokens, bucket.reserved_tokens, bucket.used_calls) == (12, 0, 1)


async def test_reservations_cannot_exceed_immutable_authorized_overage_cap(billing):
    await admit(billing.run, estimate=105)
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run, estimate=6)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "billing_overage_limit" and not row.reservation_active
    for bucket in await buckets(billing):
        assert (bucket.reserved_tokens, bucket.reserved_calls) == (105, 1)
    assert await RunEvent.objects.filter(run=billing.run, event_type="llm.quota_denied").aexists()


async def test_actual_overdraw_is_preserved_and_stops_further_admission(billing):
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="succeeded", usage={"total_tokens": 111})
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run, estimate=0)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "billing_overage_limit"
    for bucket in await buckets(billing):
        assert bucket.used_tokens == 111 and bucket.reserved_tokens == 0


async def test_owner_hard_quota_remains_independent_of_subscription(billing):
    billing.quota.request_limit = 0
    await billing.quota.asave()
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "quota_exceeded"
    await billing.period.arefresh_from_db()
    assert billing.period.reserved_tokens == billing.period.reserved_calls == 0


async def test_subscription_alone_tracks_usage_without_an_owner_quota(billing):
    await billing.quota.adelete()
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="succeeded", usage=USAGE)
    row = await LLMInvocation.objects.aget(pk=reservation.id)
    assert row.quota_window_id is None and row.billing_period_id == billing.period.pk
    await billing.period.arefresh_from_db()
    assert (billing.period.used_tokens, billing.period.used_calls) == (12, 1)


async def test_entitlements_use_frozen_run_owner_after_workflow_owner_changes(billing):
    other = await get_user_model().objects.acreate(username="unsubscribed-new-workflow-owner")
    await Workflow.objects.filter(pk=billing.run.workflow_id).aupdate(owner=other)
    reservation = await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=reservation.id)
    assert row.owner_id == billing.owner.pk and row.billing_period_id == billing.period.pk


@pytest.mark.parametrize(
    "state,code",
    [
        ("missing_owner", "billing_owner_required"),
        ("missing_account", "billing_account_required"),
        ("wrong_mode", "billing_account_required"),
        ("blocked", "billing_account_blocked"),
        ("pending", "billing_subscription_inactive"),
        ("canceled", "billing_subscription_inactive"),
        ("unpaid", "billing_paid_period_required"),
        ("future", "billing_paid_period_required"),
        ("expired", "billing_paid_period_required"),
        ("closed", "billing_paid_period_required"),
    ],
)
async def test_missing_or_inactive_entitlements_deny_before_dispatch(
    billing, settings, state, code
):
    if state == "missing_owner":
        await WorkflowRun.objects.filter(pk=billing.run.pk).aupdate(billing_owner=None)
    elif state == "missing_account":
        other = await get_user_model().objects.acreate(username="no-account")
        await WorkflowRun.objects.filter(pk=billing.run.pk).aupdate(billing_owner=other)
    elif state == "wrong_mode":
        # Arrange another environment without changing the immutable persisted account.
        settings.BILLING_MODE = "live"
    elif state == "blocked":
        billing.account.blocked = True
        await billing.account.asave()
    elif state in {"pending", "canceled"}:
        billing.subscription.status = state
        await billing.subscription.asave()
    else:
        changes = {
            "unpaid": {"paid_at": None},
            "future": {"starts_at": timezone.now() + timedelta(days=1)},
            "expired": {"ends_at": timezone.now()},
            "closed": {"closed_at": timezone.now()},
        }[state]
        # Fixture setup represents an already-issued period; production mutations use domain services.
        await BillingPeriod.objects.filter(pk=billing.period.pk).aupdate(**changes)
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == code and row.status == "quota_denied"
    assert not row.reservation_active
    await billing.period.arefresh_from_db()
    assert billing.period.reserved_tokens == billing.period.reserved_calls == 0


async def test_scheduled_cancellation_honors_current_paid_period(billing):
    billing.subscription.cancel_at_period_end = True
    await billing.subscription.asave()
    reservation = await admit(billing.run)
    billing.account.blocked = True
    await billing.account.asave()
    # The reservation was already admitted before access was revoked.
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="succeeded", usage=USAGE)
    with pytest.raises(QuotaExceeded):
        await admit(billing.run)


async def test_disabled_billing_keeps_legacy_quota_behavior(billing, settings):
    settings.BILLING_ENABLED = False
    billing.account.blocked = True
    await billing.account.asave()
    reservation = await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=reservation.id)
    assert row.billing_period_id is None and row.quota_window_id is not None
    await services.finalize(row.pk, status="cancelled")
    await billing.period.arefresh_from_db()
    assert billing.period.reserved_tokens == billing.period.reserved_calls == 0


async def test_period_expiry_is_rechecked_at_serialized_admission(billing, monkeypatch):
    before_wait = billing.period.ends_at - timedelta(seconds=1)
    after_wait = billing.period.ends_at + timedelta(seconds=1)
    observations = iter((before_wait, after_wait))
    # The run starts admission before expiry; account serialization can delay it
    # until after expiry. Later event/model timestamps use the advanced clock.
    monkeypatch.setattr(timezone, "now", lambda: next(observations, after_wait))
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "billing_paid_period_required"
    await billing.period.arefresh_from_db()
    assert billing.period.reserved_tokens == billing.period.reserved_calls == 0


async def new_period_after_boundary(billing, monkeypatch):
    now = billing.period.ends_at + timedelta(seconds=1)
    monkeypatch.setattr(timezone, "now", lambda: now)
    return await BillingPeriod.objects.acreate(
        subscription=billing.subscription,
        plan=billing.plan,
        starts_at=billing.period.ends_at,
        ends_at=billing.period.ends_at + timedelta(days=30),
        paid_at=now,
        overage_limit_minor=10,
    )


async def test_unresolved_zero_estimate_old_call_blocks_then_releases_next_period(
    billing, monkeypatch
):
    reservation = await admit(billing.run, estimate=0)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="unknown")
    await billing.period.arefresh_from_db()
    assert billing.period.reserved_tokens == billing.period.reserved_calls == 0
    following = await new_period_after_boundary(billing, monkeypatch)
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run, estimate=0)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "billing_outstanding_debt"
    await services.reconcile(reservation.id, not_sent=True)
    accepted = await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=accepted.id)
    assert row.billing_period_id == following.pk
    await billing.period.arefresh_from_db()
    assert billing.period.used_calls == 0


async def test_paid_base_invoice_does_not_hide_unpaid_overage_debt(billing, monkeypatch):
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="succeeded", usage={"total_tokens": 111})
    await Invoice.objects.acreate(
        period=billing.period,
        kind="base",
        status="paid",
        amount_minor=100,
        currency="USD",
    )
    overage = await Invoice.objects.acreate(
        period=billing.period,
        kind="overage",
        status="open",
        amount_minor=10,
        currency="USD",
    )
    await new_period_after_boundary(billing, monkeypatch)
    with pytest.raises(QuotaExceeded) as error:
        await admit(billing.run)
    row = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert row.error_code == "billing_outstanding_debt"
    overage.status = "paid"
    await overage.asave()
    await admit(billing.run)


async def test_zero_cap_unknown_old_period_settles_original_bucket_after_rollover(
    billing, monkeypatch
):
    # Zero-cap plan cannot create collectible overage debt.
    await BillingPeriod.objects.filter(pk=billing.period.pk).aupdate(overage_limit_minor=0)
    reservation = await admit(billing.run)
    await services.mark_dispatched(reservation.id)
    await services.finalize(reservation.id, status="unknown")
    following = await new_period_after_boundary(billing, monkeypatch)
    second = await admit(billing.run)
    await services.reconcile(reservation.id, total_tokens=12)
    await billing.period.arefresh_from_db()
    await following.arefresh_from_db()
    assert (billing.period.used_tokens, billing.period.reserved_tokens) == (12, 0)
    assert (following.used_tokens, following.reserved_tokens) == (0, 20)
    assert (await LLMInvocation.objects.aget(pk=second.id)).billing_period_id == following.pk


@pytest.mark.postgresql
async def test_independent_runs_race_one_shared_paid_allowance(billing):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock behavior requires PostgreSQL")
    other = await WorkflowRun.objects.acreate(
        workflow_id=billing.run.workflow_id,
        billing_owner=billing.owner,
        status="running",
        workflow_snapshot={"limits": {"max_tokens": 10_000}},
    )
    barrier = Barrier(2)

    def worker(run_id):
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            try:
                return async_to_sync(admit)(SimpleNamespace(pk=run_id), estimate=105).id
            except QuotaExceeded:
                return None
        finally:
            connections.close_all()

    def race():
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, [billing.run.pk, other.pk]))

    results = await asyncio.to_thread(race)
    assert sum(result is not None for result in results) == 1
    for bucket in await buckets(billing):
        assert (bucket.reserved_tokens, bucket.reserved_calls) == (105, 1)
    assert await LLMInvocation.objects.filter(error_code="billing_overage_limit").acount() == 1
