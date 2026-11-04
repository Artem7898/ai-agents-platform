"""Quota accounting survives independent transactions and uncertain provider outcomes."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
import pytest_asyncio
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.utils import timezone

from src.runs.exceptions import CancellationRequested, ExecutionLeaseLost, RunStateConflict
from src.runs.models import RunEvent, WorkflowRun
from src.usage import services
from src.usage.contracts import InvocationContext, QuotaExceeded
from src.usage.models import LLMInvocation, LLMRate, OwnerQuota, QuotaWindow
from src.workflows.models import Workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]
USAGE = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}


@pytest_asyncio.fixture
async def account():
    owner = await get_user_model().objects.acreate(username=f"ledger-{uuid.uuid4()}")
    workflow = await Workflow.objects.acreate(
        name="Quota workflow",
        owner=owner,
        nodes=[{"id": "start", "kind": "hitl_approval"}],
        edges=[],
    )
    run = await WorkflowRun.objects.acreate(
        workflow=workflow, billing_owner=owner, status="running"
    )
    quota = await OwnerQuota.objects.acreate(owner=owner, token_limit=100, request_limit=5)
    return owner, run, quota


async def admit(run, *, logical_call=1, estimate=20, token=None):
    return await services.reserve(
        InvocationContext(run.pk, "node", logical_call, token),
        model_name="test-model",
        provider="openai",
        estimated_tokens=estimate,
        max_output_tokens=10,
        operation="chat",
    )


async def test_denial_commits_row_event_and_preserves_counters(account):
    _, run, quota = account
    quota.token_limit = 19
    await quota.asave()
    with pytest.raises(QuotaExceeded) as error:
        await admit(run)
    invocation = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    window = await QuotaWindow.objects.aget(quota=quota)
    assert invocation.status == "quota_denied"
    assert not invocation.reservation_active
    assert (window.reserved_calls, window.used_calls, window.reserved_tokens) == (0, 0, 0)
    assert await RunEvent.objects.filter(run=run, event_type="llm.quota_denied").aexists()


async def test_success_is_idempotent_and_each_dispatch_is_a_request(account):
    _, run, quota = account
    reserved = await admit(run)
    await services.mark_dispatched(reserved.id)
    await services.mark_dispatched(reserved.id)
    await services.finalize(reserved.id, status="succeeded", usage=USAGE)
    await services.finalize(reserved.id, status="succeeded", usage=USAGE)
    window = await QuotaWindow.objects.aget(quota=quota)
    invocation = await LLMInvocation.objects.aget(pk=reserved.id)
    assert (
        window.used_tokens,
        window.reserved_tokens,
        window.used_calls,
        window.reserved_calls,
    ) == (10, 0, 1, 0)
    assert invocation.usage_status == "reported"
    assert invocation.cost is None
    assert not invocation.reservation_active
    with pytest.raises(ValueError, match="cannot be overwritten"):
        await services.finalize(
            reserved.id, status="succeeded", usage={**USAGE, "total_tokens": 11}
        )
    second = await admit(run)
    assert second.attempt == 2


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "unknown"])
async def test_dispatched_missing_usage_keeps_hold_and_null_metrics(account, status):
    _, run, quota = account
    reserved = await admit(run)
    await services.mark_dispatched(reserved.id)
    await services.finalize(reserved.id, status=status)
    invocation = await LLMInvocation.objects.aget(pk=reserved.id)
    window = await QuotaWindow.objects.aget(quota=quota)
    assert invocation.usage_status == "unknown"
    assert (
        invocation.total_tokens is invocation.prompt_tokens is invocation.completion_tokens is None
    )
    assert invocation.cost is None
    assert invocation.reservation_active
    assert (window.reserved_tokens, window.used_tokens, window.used_calls) == (20, 0, 1)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_unsent_attempt_releases_both_reservations(account, status):
    _, run, quota = account
    reserved = await admit(run)
    await services.finalize(reserved.id, status=status)
    window = await QuotaWindow.objects.aget(quota=quota)
    assert (
        window.reserved_tokens,
        window.used_tokens,
        window.used_calls,
        window.reserved_calls,
    ) == (0, 0, 0, 0)


async def test_partial_usage_retained_total_only_settles_and_missing_total_holds(account):
    _, run, quota = account
    first = await admit(run)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="succeeded", usage={"prompt_tokens": 4})
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.prompt_tokens == 4 and row.total_tokens is None and row.reservation_active
    assert row.usage_status == "partial" and row.cost is None
    second = await admit(run, logical_call=2)
    await services.mark_dispatched(second.id)
    await services.finalize(second.id, status="succeeded", usage={"total_tokens": 10})
    row = await LLMInvocation.objects.aget(pk=second.id)
    window = await QuotaWindow.objects.aget(quota=quota)
    assert row.total_tokens == 10 and row.prompt_tokens is None
    assert not row.reservation_active and row.cost is None
    assert (window.reserved_tokens, window.used_tokens) == (20, 10)


async def test_malformed_huge_or_inconsistent_usage_is_unknown(account):
    _, run, _ = account
    first = await admit(run)
    await services.mark_dispatched(first.id)
    await services.finalize(
        first.id, status="succeeded", usage={"total_tokens": 2**100, "prompt_tokens": True}
    )
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.total_tokens is row.prompt_tokens is None and row.reservation_active
    second = await admit(run, logical_call=2)
    await services.mark_dispatched(second.id)
    await services.finalize(
        second.id,
        status="succeeded",
        usage={"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 1},
    )
    row = await LLMInvocation.objects.aget(pk=second.id)
    assert row.prompt_tokens == 5 and row.completion_tokens == 8
    assert row.total_tokens is None and row.reservation_active


async def test_actual_over_estimate_is_fully_charged_and_blocks_admission(account):
    _, run, quota = account
    quota.token_limit = 15
    await quota.asave()
    first = await admit(run, estimate=5)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="succeeded", usage={"total_tokens": 25})
    window = await QuotaWindow.objects.aget(quota=quota)
    assert (window.used_tokens, window.reserved_tokens) == (25, 0)
    with pytest.raises(QuotaExceeded):
        await admit(run, estimate=1)


async def test_original_token_can_settle_after_lease_changes_but_cannot_admit(account):
    _, run, _ = account
    original = uuid.uuid4()
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        execution_token=original, lease_expires_at=timezone.now() + timedelta(minutes=1)
    )
    first = await admit(run, token=original)
    await services.mark_dispatched(first.id, execution_token=original)
    replacement = uuid.uuid4()
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        execution_token=replacement, lease_expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(ExecutionLeaseLost):
        await admit(run, token=original)
    with pytest.raises(ExecutionLeaseLost):
        await services.finalize(
            first.id, status="succeeded", usage=USAGE, execution_token=replacement
        )
    await services.finalize(first.id, status="succeeded", usage=USAGE, execution_token=original)
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.total_tokens == 10 and not row.reservation_active


async def test_reconciliation_rejects_active_lease_then_settles_once(account):
    _, run, quota = account
    token = uuid.uuid4()
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        execution_token=token, lease_expires_at=timezone.now() + timedelta(minutes=1)
    )
    first = await admit(run, token=token)
    await services.mark_dispatched(first.id, execution_token=token)
    await services.finalize(first.id, status="unknown", execution_token=token)
    with pytest.raises(ValueError, match="active execution lease"):
        await services.reconcile(first.id, **USAGE)
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    row = await services.reconcile(first.id, **USAGE)
    assert row.usage_status == "reconciled"
    await services.reconcile(first.id, **USAGE)
    with pytest.raises(ValueError, match="conflicts"):
        await services.reconcile(first.id, **{**USAGE, "total_tokens": 11})
    window = await QuotaWindow.objects.aget(quota=quota)
    assert (window.used_tokens, window.reserved_tokens, window.used_calls) == (10, 0, 1)


async def test_staff_confirmed_not_sent_releases_stale_hold(account):
    _, run, quota = account
    first = await admit(run)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="unknown")
    await services.reconcile(first.id, not_sent=True)
    await services.reconcile(first.id, not_sent=True)
    window = await QuotaWindow.objects.aget(quota=quota)
    assert (
        window.used_tokens,
        window.reserved_tokens,
        window.used_calls,
        window.reserved_calls,
    ) == (0, 0, 0, 0)


async def test_window_rollover_preserves_original_unknown_hold_and_anchor(account, monkeypatch):
    _, run, quota = account
    quota.request_limit = 1
    await quota.asave()
    first = await admit(run)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="unknown")
    anchored = quota.created_at
    monkeypatch.setattr(
        services.timezone, "now", lambda: anchored + timedelta(seconds=quota.period_seconds + 1)
    )
    second = await admit(run, logical_call=2)
    old = await LLMInvocation.objects.aget(pk=first.id)
    new = await LLMInvocation.objects.aget(pk=second.id)
    assert old.quota_window_id != new.quota_window_id
    assert await QuotaWindow.objects.filter(quota=quota, reserved_tokens=20).acount() == 2
    quota.period_seconds += 1
    with pytest.raises(ValidationError, match="immutable"):
        await quota.asave()


async def test_price_uses_frozen_decimal_tariff_and_no_content_is_persisted(account):
    _, run, _ = account
    rate = await LLMRate.objects.acreate(
        model_name="test-model",
        version="v1",
        currency="USD",
        input_per_million=Decimal("2.50"),
        output_per_million=Decimal("10.00"),
    )
    first = await admit(run)
    await LLMRate.objects.acreate(
        model_name="test-model",
        version="v2",
        currency="USD",
        input_per_million=Decimal("100.00"),
        output_per_million=Decimal("100.00"),
    )
    await services.mark_dispatched(first.id)
    await services.finalize(
        first.id,
        status="succeeded",
        usage=USAGE,
        error_code="secret body with spaces",
        provider_request_id="bad request body",
    )
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.rate_id == rate.pk and row.rate_version == "v1"
    assert row.cost == Decimal("0.000070000000000000")
    assert row.error_code == "provider_error" and row.provider_request_id is None
    payloads = [event.payload async for event in RunEvent.objects.filter(run=run)]
    assert "secret body" not in str(payloads)
    rate.input_per_million = Decimal("1000")
    with pytest.raises(ValidationError, match="immutable"):
        await rate.asave()


async def test_missing_owner_or_quota_still_tracks_without_bucket(account):
    _, run, _ = account
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(billing_owner=None)
    first = await admit(run)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="succeeded", usage=USAGE)
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.owner_id is None and row.quota_window_id is None and row.total_tokens == 10


async def test_admission_requires_running_not_cancelled_and_dispatch_rechecks(account):
    _, run, _ = account
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(status="completed")
    with pytest.raises(RunStateConflict):
        await admit(run)
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(status="running")
    first = await admit(run)
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(cancel_requested=True)
    with pytest.raises(CancellationRequested):
        await admit(run)
    with pytest.raises(CancellationRequested):
        await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="cancelled")


@pytest.mark.postgresql
async def test_independent_postgres_transactions_share_one_owner_bucket(account):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock behavior requires PostgreSQL")
    owner, run, quota = account
    quota.request_limit = 1
    await quota.asave()
    other_run = await WorkflowRun.objects.acreate(
        workflow_id=run.workflow_id, billing_owner=owner, status="running"
    )
    barrier = Barrier(2)

    def worker(run_id):
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            try:
                result = async_to_sync(services.reserve)(
                    InvocationContext(run_id, "node", 1),
                    model_name="test-model",
                    provider="openai",
                    estimated_tokens=20,
                    max_output_tokens=10,
                    operation="chat",
                )
                return result.id
            except QuotaExceeded:
                return None
        finally:
            connections.close_all()

    def race():
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, [run.pk, other_run.pk]))

    results = await asyncio.to_thread(race)
    assert sum(value is not None for value in results) == 1
    window = await QuotaWindow.objects.aget(quota=quota)
    assert (window.reserved_calls, window.reserved_tokens) == (1, 20)
    assert await LLMInvocation.objects.filter(status="quota_denied").acount() == 1
    assert await RunEvent.objects.filter(event_type="llm.quota_denied").acount() == 1


async def test_run_budget_counts_failed_and_unknown_attempts_and_ignores_checkpoint_lag(account):
    _, run, quota = account
    quota.enabled = False
    await quota.asave()
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        workflow_snapshot={"limits": {"max_tokens": 25}}
    )
    first = await admit(run, estimate=20)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="failed")
    with pytest.raises(QuotaExceeded) as error:
        await admit(run, estimate=10)
    denied = await LLMInvocation.objects.aget(pk=error.value.invocation_id)
    assert denied.error_code == "run_token_limit"
    assert await sync_to_async(services.budget_tokens_for_run)(run.pk) == 20
    await services.reconcile(first.id, total_tokens=5)
    second = await admit(run, estimate=20)
    assert second.attempt == 3


async def test_run_budget_honors_legacy_baseline_without_double_counting(account):
    _, run, _ = account
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        workflow_snapshot={"limits": {"max_tokens": 40}},
        checkpoint={"usage": {"ledger_base_tokens": 10, "budget_tokens": 30, "total_tokens": 10}},
    )
    first = await admit(run, estimate=20)
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="succeeded", usage={"total_tokens": 20})
    with pytest.raises(QuotaExceeded):
        await admit(run, estimate=11)
    allowed = await admit(run, estimate=10)
    assert allowed.attempt == 3


async def test_maximum_valid_price_and_tokens_do_not_overflow_settlement(account):
    _, run, quota = account
    quota.enabled = False
    await quota.asave()
    rate_value = Decimal("999999999999.99999999")
    await LLMRate.objects.acreate(
        model_name="test-model",
        version="max",
        currency="USD",
        input_per_million=rate_value,
        output_per_million=rate_value,
    )
    first = await admit(run)
    await services.mark_dispatched(first.id)
    count = 2**63 - 1
    await services.finalize(
        first.id,
        status="succeeded",
        usage={"prompt_tokens": count, "completion_tokens": 0, "total_tokens": count},
    )
    row = await LLMInvocation.objects.aget(pk=first.id)
    assert row.total_tokens == count and not row.reservation_active
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 64
        assert row.cost == Decimal(count) * rate_value / Decimal(1_000_000)


async def test_first_reservation_durably_freezes_legacy_usage_before_crash(account):
    _, run, _ = account
    await WorkflowRun.objects.filter(pk=run.pk).aupdate(
        workflow_snapshot={"limits": {"max_tokens": 40}},
        checkpoint={"usage": {"total_tokens": 15}},
    )
    first = await admit(run, estimate=20)
    await run.arefresh_from_db()
    assert run.checkpoint["usage"]["ledger_base_tokens"] == 15
    # The old worker never checkpoints its completed provider call.
    await services.mark_dispatched(first.id)
    await services.finalize(first.id, status="succeeded", usage={"total_tokens": 10})
    await admit(run, estimate=10)
    with pytest.raises(QuotaExceeded):
        await admit(run, estimate=6)
