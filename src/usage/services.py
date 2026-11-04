"""Atomic quota admission and settlement, without transactions over provider I/O."""

import copy
import re
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import BigIntegerField, Case, F, Max, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from src.runs.exceptions import CancellationRequested, ExecutionLeaseLost, RunStateConflict
from src.runs.models import WorkflowRun
from src.runs.persistence import _append_locked, _assert_lease

from .contracts import InvocationContext, QuotaExceeded, Reservation
from .models import LLMInvocation, LLMRate, OwnerQuota, QuotaWindow

_FINAL_STATUSES = {"succeeded", "failed", "cancelled", "unknown"}
_METRICS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _usage(values: dict[str, Any] | None) -> dict[str, int | None]:
    values = values if isinstance(values, dict) else {}
    result = {
        field: value if type(value := values.get(field)) is int and 0 <= value < 2**63 else None
        for field in _METRICS
    }
    known_parts = (result["prompt_tokens"] or 0) + (result["completion_tokens"] or 0)
    if result["total_tokens"] is not None and result["total_tokens"] < known_parts:
        result["total_tokens"] = None
    return result


def _safe_identifier(value: str | None, maximum: int, *, fallback=None):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1," + str(maximum) + "}", value):
        return value
    return fallback


def _event(run, invocation, event_type):
    _append_locked(
        run,
        event_type,
        {
            "invocation_id": str(invocation.pk),
            "node_id": invocation.node_id,
            "logical_call": invocation.logical_call,
            "attempt": invocation.attempt,
            "runtime_id": str(invocation.runtime_id) if invocation.runtime_id else None,
            "model_name": invocation.model_name,
            "status": invocation.status,
            "usage_status": invocation.usage_status,
            "reserved_tokens": invocation.reserved_tokens,
            **{field: getattr(invocation, field) for field in _METRICS},
        },
        None,
    )


def _window(quota, now):
    offset = max(0, int((now - quota.created_at).total_seconds()) // quota.period_seconds)
    starts_at = quota.created_at + timedelta(seconds=offset * quota.period_seconds)
    window, _ = QuotaWindow.objects.get_or_create(
        quota=quota,
        starts_at=starts_at,
        defaults={"ends_at": starts_at + timedelta(seconds=quota.period_seconds)},
    )
    return QuotaWindow.objects.select_for_update().get(pk=window.pk)


def _locked(invocation_id):
    # Discover the immutable relationships before locking in the common order.
    reference = LLMInvocation.objects.only("run_id", "quota_window_id").get(pk=invocation_id)
    run = WorkflowRun.objects.select_for_update().get(pk=reference.run_id)
    window = None
    if reference.quota_window_id:
        quota_id = QuotaWindow.objects.values_list("quota_id", flat=True).get(
            pk=reference.quota_window_id
        )
        OwnerQuota.objects.select_for_update().get(pk=quota_id)
        window = QuotaWindow.objects.select_for_update().get(pk=reference.quota_window_id)
    invocation = LLMInvocation.objects.select_for_update().get(pk=invocation_id)
    return run, window, invocation


def _assert_invocation_token(invocation, token):
    # A provider response can arrive after lease expiry or worker replacement.
    if invocation.execution_token != token:
        raise ExecutionLeaseLost("The invocation belongs to another execution token")


def _release(invocation, window, total=None, *, not_sent=False):
    if not invocation.reservation_active:
        return
    if window:
        window.reserved_tokens -= invocation.reserved_tokens
        if invocation.dispatched_at is None:
            window.reserved_calls -= 1
            if not not_sent:
                window.used_calls += 1
        if total is not None:
            # Never clamp actual usage to the estimate: overdrawn windows block admission.
            window.used_tokens += total
        window.save(
            update_fields=["reserved_tokens", "reserved_calls", "used_tokens", "used_calls"]
        )
    invocation.reservation_active = False


def _price(invocation):
    if (
        invocation.input_per_million is not None
        and invocation.output_per_million is not None
        and invocation.prompt_tokens is not None
        and invocation.completion_tokens is not None
    ):
        with localcontext() as context:
            context.prec = 64
            invocation.cost = (
                Decimal(invocation.prompt_tokens) * invocation.input_per_million
                + Decimal(invocation.completion_tokens) * invocation.output_per_million
            ) / Decimal(1_000_000)


def budget_tokens_for_run(run_id: UUID) -> int:
    """Synchronous aggregate; admission calls this while holding the parent run lock."""
    return (
        LLMInvocation.objects.filter(run_id=run_id)
        .exclude(status="quota_denied")
        .aggregate(
            tokens=Coalesce(
                Sum(
                    Case(
                        When(reservation_active=True, then=F("reserved_tokens")),
                        default=Coalesce(F("total_tokens"), Value(0)),
                        output_field=BigIntegerField(),
                    )
                ),
                Value(0),
                output_field=BigIntegerField(),
            )
        )["tokens"]
    )


@sync_to_async(thread_sensitive=True)
def reserve(
    context: InvocationContext,
    *,
    model_name: str,
    runtime_id: UUID | None = None,
    provider: str,
    estimated_tokens: int,
    max_output_tokens: int,
    operation: str,
) -> Reservation:
    """Reserve a total token estimate and one transport attempt before dispatch."""
    for value in (estimated_tokens, max_output_tokens, context.logical_call):
        if type(value) is not int or value < 0 or value >= 2**63:
            raise ValueError("Token budgets and logical call numbers must be nonnegative integers")
    if not context.node_id or len(context.node_id) > 255:
        raise ValueError("A bounded node identifier is required")
    if not model_name or len(model_name) > 255 or not provider or len(provider) > 32:
        raise ValueError("A bounded model and provider are required")
    if not operation or len(operation) > 32:
        raise ValueError("A bounded operation is required")
    denied = False
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(pk=context.run_id)
        _assert_lease(run, context.execution_token)
        if run.cancel_requested:
            raise CancellationRequested("The run owner requested cancellation")
        if run.status != "running":
            raise RunStateConflict("Only running workflows can reserve LLM quota")
        now = timezone.now()
        checkpoint_usage = run.checkpoint.get("usage", {})
        existing_calls = (
            LLMInvocation.objects.filter(run=run).exclude(status="quota_denied").exists()
        )
        baseline = checkpoint_usage.get("ledger_base_tokens")
        if baseline is None:
            baseline = (
                0
                if existing_calls
                else checkpoint_usage.get("budget_tokens", checkpoint_usage.get("total_tokens", 0))
            )
        baseline = baseline if type(baseline) is int and baseline >= 0 else 0
        if "ledger_base_tokens" not in checkpoint_usage and run.checkpoint:
            checkpoint = copy.deepcopy(run.checkpoint)
            checkpoint.setdefault("usage", {})["ledger_base_tokens"] = baseline
            run.checkpoint = checkpoint
            run.save(update_fields=["checkpoint", "updated_at"])
        run_limit = run.workflow_snapshot.get("limits", {}).get("max_tokens", 32000)
        if type(run_limit) is not int or run_limit < 1:
            raise ValueError("The run has an invalid token budget")
        run_denied = baseline + budget_tokens_for_run(run.pk) + estimated_tokens > run_limit
        owner_id = run.billing_owner_id
        quota = (
            OwnerQuota.objects.select_for_update().filter(owner_id=owner_id).first()
            if owner_id
            else None
        )
        window = _window(quota, now) if quota and quota.enabled else None
        denied = run_denied
        if window:
            assert quota is not None
            denied = (
                denied
                or (
                    quota.token_limit is not None
                    and window.used_tokens + window.reserved_tokens + estimated_tokens
                    > quota.token_limit
                )
                or (
                    quota.request_limit is not None
                    and window.used_calls + window.reserved_calls + 1 > quota.request_limit
                )
            )
        attempt = (
            LLMInvocation.objects.filter(
                run=run, node_id=context.node_id, logical_call=context.logical_call
            ).aggregate(value=Max("attempt"))["value"]
            or 0
        ) + 1
        rate = (
            LLMRate.objects.filter(
                runtime_id=runtime_id, model_name=model_name, effective_from__lte=now
            )
            .order_by("-effective_from", "-created_at")
            .first()
        )
        runtime_revision = None
        if runtime_id:
            # Frozen revision belongs to the run snapshot, not the mutable runtime record.
            snapshot = run.runtime_snapshots.get(str(runtime_id), {})
            if isinstance(snapshot, dict):
                runtime_revision = snapshot.get("revision")
        invocation = LLMInvocation.objects.create(
            run=run,
            owner_id=owner_id,
            node_id=context.node_id,
            logical_call=context.logical_call,
            attempt=attempt,
            runtime_id=runtime_id,
            runtime_revision=runtime_revision,
            model_name=model_name,
            provider=provider,
            operation=operation,
            execution_token=context.execution_token,
            quota_window=window,
            reserved_tokens=estimated_tokens,
            max_output_tokens=max_output_tokens,
            reservation_active=not denied,
            status="quota_denied" if denied else "reserved",
            finished_at=now if denied else None,
            error_code=("run_token_limit" if run_denied else "quota_exceeded") if denied else None,
            rate=rate,
            rate_version=rate.version if rate else None,
            currency=rate.currency if rate else None,
            input_per_million=rate.input_per_million if rate else None,
            output_per_million=rate.output_per_million if rate else None,
            started_at=now,
        )
        if window and not denied:
            window.reserved_tokens += estimated_tokens
            window.reserved_calls += 1
            window.save(update_fields=["reserved_tokens", "reserved_calls"])
        _event(run, invocation, "llm.quota_denied" if denied else "llm.reserved")
    # Outside atomic: rejection evidence must survive the exception.
    if denied:
        raise QuotaExceeded(invocation.pk, scope="run" if run_denied else "owner")
    return Reservation(invocation.pk, estimated_tokens, attempt)


@sync_to_async(thread_sensitive=True)
def mark_dispatched(invocation_id: UUID, execution_token: UUID | None = None) -> None:
    with transaction.atomic():
        run, window, invocation = _locked(invocation_id)
        _assert_invocation_token(invocation, execution_token)
        _assert_lease(run, execution_token)
        if invocation.dispatched_at is not None:
            return
        if run.cancel_requested:
            raise CancellationRequested("The run owner requested cancellation")
        if run.status != "running":
            raise RunStateConflict("Only running workflows can dispatch LLM calls")
        if invocation.status != "reserved" or not invocation.reservation_active:
            raise ValueError("Only an admitted reservation can be dispatched")
        invocation.dispatched_at = timezone.now()
        invocation.status = "dispatched"
        if window:
            window.reserved_calls -= 1
            window.used_calls += 1
            window.save(update_fields=["reserved_calls", "used_calls"])
        invocation.save(update_fields=["dispatched_at", "status"])
        _event(run, invocation, "llm.dispatched")


@sync_to_async(thread_sensitive=True)
def finalize(
    invocation_id: UUID,
    *,
    status: str,
    usage: dict[str, Any] | None = None,
    provider_request_id: str | None = None,
    error_code: str | None = None,
    execution_token: UUID | None = None,
) -> None:
    if status not in _FINAL_STATUSES:
        raise ValueError("Invalid invocation settlement status")
    metrics = _usage(usage)
    with transaction.atomic():
        run, window, invocation = _locked(invocation_id)
        _assert_invocation_token(invocation, execution_token)
        if invocation.status == "quota_denied":
            raise ValueError("A denied invocation cannot be settled")
        existing = {field: getattr(invocation, field) for field in _METRICS}
        if invocation.finished_at is not None:
            if invocation.status != status:
                raise ValueError("An invocation settlement cannot change its outcome")
            for field, value in metrics.items():
                if existing[field] is not None and value is not None and existing[field] != value:
                    raise ValueError("Reported usage cannot be overwritten")
            if not any(
                value is not None and existing[field] is None for field, value in metrics.items()
            ):
                return
        if invocation.dispatched_at is None and (
            status not in {"failed", "cancelled"}
            or any(value is not None for value in metrics.values())
        ):
            raise ValueError("An undispatched invocation cannot report provider usage")
        for field, value in metrics.items():
            if value is not None:
                setattr(invocation, field, value)
        known_parts = (invocation.prompt_tokens or 0) + (invocation.completion_tokens or 0)
        if invocation.total_tokens is not None and invocation.total_tokens < known_parts:
            raise ValueError("Reported usage conflicts with known token components")
        invocation.status = status
        invocation.finished_at = invocation.finished_at or timezone.now()
        invocation.provider_request_id = (
            _safe_identifier(provider_request_id, 128) or invocation.provider_request_id
        )
        invocation.error_code = (
            _safe_identifier(error_code, 64, fallback="provider_error") or invocation.error_code
        )
        if invocation.dispatched_at is None:
            _release(invocation, window, not_sent=True)
        elif invocation.total_tokens is not None:
            _release(invocation, window, invocation.total_tokens)
        known = sum(getattr(invocation, field) is not None for field in _METRICS)
        invocation.usage_status = "reported" if known == 3 else "partial" if known else "unknown"
        _price(invocation)
        invocation.save()
        _event(run, invocation, "llm.finalized")


@sync_to_async(thread_sensitive=True)
def reconcile(
    invocation_id: UUID,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    not_sent: bool = False,
) -> LLMInvocation:
    """Settle stale unknown usage from external evidence, never an active worker."""
    supplied: dict[str, Any] = dict(
        zip(_METRICS, (prompt_tokens, completion_tokens, total_tokens), strict=True)
    )
    if type(not_sent) is not bool or (not_sent and any(v is not None for v in supplied.values())):
        raise ValueError("Choose either usage evidence or confirmation that the call was not sent")
    if not not_sent and (total_tokens is None or _usage(supplied) != supplied):
        raise ValueError("Reconciliation requires valid usage including a total")
    with transaction.atomic():
        run, window, invocation = _locked(invocation_id)
        if invocation.usage_status == "reconciled":
            if not_sent:
                if invocation.error_code == "reconciled_not_sent":
                    return invocation
            elif all(getattr(invocation, field) == value for field, value in supplied.items()):
                return invocation
            raise ValueError("Reconciliation evidence conflicts with the existing settlement")
        if invocation.usage_status == "reported" or invocation.status == "quota_denied":
            raise ValueError("Known reported usage cannot be reconciled")
        if (
            run.execution_token is not None
            and run.execution_token == invocation.execution_token
            and run.lease_expires_at is not None
            and run.lease_expires_at > timezone.now()
        ):
            raise ValueError("The invocation still has an active execution lease")
        if not_sent:
            if any(getattr(invocation, field) is not None for field in _METRICS):
                raise ValueError("Reported usage conflicts with a not-sent assertion")
            # A confirmed non-send can reverse the conservative dispatch marker.
            if window and invocation.dispatched_at and invocation.reservation_active:
                window.used_calls -= 1
                window.reserved_calls += 1
                window.save(update_fields=["used_calls", "reserved_calls"])
            invocation.dispatched_at = None
            _release(invocation, window, not_sent=True)
            invocation.status = "cancelled"
            invocation.error_code = "reconciled_not_sent"
        else:
            for field, value in supplied.items():
                existing = getattr(invocation, field)
                if existing is not None and value != existing:
                    raise ValueError("Reconciliation cannot overwrite reported usage")
                setattr(invocation, field, value)
            _release(invocation, window, total_tokens)
            if invocation.finished_at is None:
                invocation.status = "unknown"
            _price(invocation)
        invocation.usage_status = "reconciled"
        invocation.finished_at = invocation.finished_at or timezone.now()
        invocation.save()
        _event(run, invocation, "llm.reconciled")
        return invocation
