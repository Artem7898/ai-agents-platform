"""Console charts report personal persisted data without invented measurements."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied

from src.agents.models import Agent
from src.billing.models import BillingAccount, BillingPeriod, PlanVersion, Subscription
from src.console import analytics
from src.runs.models import OutboxMessage, WorkflowRun
from src.usage.models import LLMInvocation
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 11, 6, 12, 30, tzinfo=UTC)


@pytest.fixture
def records(monkeypatch, settings):
    monkeypatch.setattr(analytics, "timezone", SimpleNamespace(now=lambda: NOW))
    settings.KNOWLEDGE_BASE_SEARCH_URL = ""
    settings.BILLING_ENABLED = False
    settings.BILLING_MODE = "test"
    user = get_user_model().objects.create(username="dashboard-owner", is_staff=True)
    other = get_user_model().objects.create(username="dashboard-other")
    agent = Agent.objects.create(
        owner=user,
        name="Research agent",
        model_name="test-model",
        status="active",
        system_prompt="prompt-secret",
        tool_ids=[],
    )
    workflow = Workflow.objects.create(
        owner=user,
        name="Current workflow",
        nodes=[
            {
                "id": "llm",
                "kind": "llm_call",
                "agent_id": str(agent.pk),
                "config": {"private": "config-secret"},
            }
        ],
        edges=[],
    )
    index = count(1)

    def run(*, owner=user, status="running", updated_at=NOW, created_at=NOW - timedelta(minutes=5)):
        instance = WorkflowRun.objects.create(
            workflow=workflow,
            billing_owner=owner,
            status=status,
            input_data={"private": "input-secret"},
            output_data={"private": "output-secret"},
            workflow_snapshot={
                "name": "Frozen workflow",
                "nodes": [{"id": "llm", "kind": "llm_call", "agent_id": str(agent.pk)}],
            },
            agent_snapshots={
                str(agent.pk): {
                    "spec": {
                        "id": str(agent.pk),
                        "name": "Frozen research",
                        "system_prompt": "prompt-secret",
                    },
                }
            },
        )
        WorkflowRun.objects.filter(pk=instance.pk).update(
            created_at=created_at, updated_at=updated_at
        )
        instance.refresh_from_db()
        return instance

    def call(
        instance,
        *,
        total=10,
        owner=user,
        node_id="llm",
        status="succeeded",
        dispatched=True,
        started_at=NOW,
        cost=None,
        currency=None,
    ):
        return LLMInvocation.objects.create(
            run=instance,
            owner=owner,
            node_id=node_id,
            logical_call=next(index),
            attempt=1,
            model_name="test-model",
            provider="openai_compatible",
            operation="completion",
            reserved_tokens=20,
            max_output_tokens=10,
            reservation_active=total is None,
            total_tokens=total,
            status=status,
            dispatched_at=NOW if dispatched else None,
            started_at=started_at,
            cost=cost,
            currency=currency,
        )

    return SimpleNamespace(
        user=user, other=other, agent=agent, workflow=workflow, run=run, call=call
    )


def test_empty_dashboard_has_dense_zero_activity_and_no_fake_health_or_knowledge(records):
    result = analytics.dashboard(records.user)
    assert result["scope"] == "personal" and result["timezone"] == "UTC"
    assert len(result["activity"]) == 24
    assert sum(row["completed"] + row["failed"] for row in result["activity"]) == 0
    assert result["metrics"]["tokens_today"] == 0
    assert result["metrics"]["system_health"]["uptime_percent"] is None
    assert result["knowledge"] == {
        "configured": False,
        "index_status": "not_connected",
        "documents_count": None,
        "memory_status": "not_available",
    }
    assert result["recent_runs"] == result["token_usage"] == result["alerts"] == []
    assert result["token_usage_complete"] is True


def test_staff_dashboard_remains_personal_and_history_uses_frozen_owner_and_name(records):
    owned = records.run()
    records.call(owned, total=12)
    foreign = records.run(owner=records.other, status="failed")
    records.call(foreign, owner=records.other, total=999)
    OutboxMessage.objects.create(run=owned, topic="run_workflow")
    OutboxMessage.objects.create(run=foreign, topic="run_workflow")
    records.workflow.owner = records.other
    records.workflow.name = "private-current-title"
    records.workflow.save()
    result = analytics.dashboard(records.user)
    assert result["metrics"]["active_runs"] == 1
    assert result["metrics"]["queued_tasks"] == 1
    assert result["metrics"]["errors_24h"] == 0
    assert result["metrics"]["tokens_today"] == 12
    assert [row["id"] for row in result["recent_runs"]] == [str(owned.pk)]
    assert result["recent_runs"][0]["workflow_name"] == "Frozen workflow"
    assert result["workflow_preview"] is None
    assert "private-current-title" not in json.dumps(result)


def test_unknown_usage_is_not_zero_and_denied_or_unsent_attempts_do_not_count(records):
    run = records.run()
    records.call(run, total=30)
    records.call(run, total=None)
    records.call(run, total=None, status="quota_denied")
    records.call(run, total=None, status="reserved", dispatched=False)
    records.call(run, total=800, started_at=NOW - timedelta(days=1))
    result = analytics.dashboard(records.user)
    assert result["metrics"]["tokens_today"] is None
    assert result["metrics"]["known_tokens_today"] == 30
    assert result["metrics"]["unknown_usage_calls_today"] == 1
    assert result["token_usage"] == [
        {
            "agent_id": str(records.agent.pk),
            "agent_name": "Frozen research",
            "tokens": 30,
            "unknown_calls": 1,
        }
    ]
    assert "unknown-usage" in {row["id"] for row in result["alerts"]}


def test_inconsistent_legacy_call_ownership_does_not_expand_the_personal_scope(records):
    foreign_run = records.run(owner=records.other)
    records.call(foreign_run, owner=records.user, total=999)
    own_run = records.run()
    records.call(own_run, owner=records.other, total=888, cost=Decimal("12"), currency="USD")
    result = analytics.dashboard(records.user)
    assert result["metrics"]["tokens_today"] == 0
    assert result["token_usage"] == []
    assert result["recent_runs"][0]["cost"] is None


def test_token_attribution_counts_each_invocation_once_and_keeps_unassigned_work(records):
    run = records.run()
    records.call(run, total=7)
    records.call(run, total=11, node_id="unmapped")
    records.agent.name = "Renamed live agent"
    records.agent.save()
    result = analytics.dashboard(records.user)
    by_name = {row["agent_name"]: row["tokens"] for row in result["token_usage"]}
    assert by_name == {"Frozen research": 7, "Unassigned": 11}
    assert sum(by_name.values()) == result["metrics"]["tokens_today"] == 18


def test_partial_attribution_chart_is_explicit_while_total_remains_exact(records, monkeypatch):
    run = records.run()
    records.call(run, total=20)
    records.call(run, total=10, node_id="unmapped")
    monkeypatch.setattr(analytics, "MAX_USAGE_GROUPS", 1)
    result = analytics.dashboard(records.user)
    assert result["token_usage_complete"] is False
    assert result["metrics"]["tokens_today"] == 30
    assert sum(row["tokens"] for row in result["token_usage"]) == 20


def test_recent_cost_does_not_mix_currencies_or_treat_missing_cost_as_free(records):
    known = records.run(status="completed")
    records.call(known, cost=Decimal("0.10"), currency="USD")
    records.call(known, cost=Decimal("0.20"), currency="USD")
    records.call(known, total=None, status="quota_denied")
    mixed = records.run(status="completed")
    records.call(mixed, cost=Decimal("0.10"), currency="USD")
    records.call(mixed, cost=Decimal("0.20"), currency="EUR")
    unknown = records.run(status="completed")
    records.call(unknown, cost=Decimal("0.10"), currency="USD")
    records.call(unknown, cost=None, currency="USD")
    recent = {row["id"]: row for row in analytics.dashboard(records.user)["recent_runs"]}
    assert Decimal(recent[str(known.pk)]["cost"]) == Decimal("0.30")
    assert recent[str(known.pk)]["currency"] == "USD"
    assert recent[str(mixed.pk)]["cost"] is recent[str(mixed.pk)]["currency"] is None
    assert recent[str(unknown.pk)]["cost"] is None


def test_activity_uses_persisted_terminal_updates_and_wall_duration(records):
    completed = records.run(
        status="completed",
        updated_at=NOW - timedelta(minutes=10),
        created_at=NOW - timedelta(minutes=20),
    )
    records.run(status="failed", updated_at=NOW - timedelta(hours=1))
    records.run(status="failed", updated_at=NOW - timedelta(hours=25))
    records.run(status="waiting_human")
    result = analytics.dashboard(records.user)
    assert result["activity"][-1]["label"] == "12:00"
    assert sum(row["completed"] for row in result["activity"]) == 1
    assert sum(row["failed"] for row in result["activity"]) == 1
    assert result["metrics"]["errors_24h"] == 1
    recent = {row["id"]: row for row in result["recent_runs"]}
    assert recent[str(completed.pk)]["duration_seconds"] == 600
    assert result["active_agents"][0]["waiting_runs"] == 1
    assert "human-approval" in {row["id"] for row in result["alerts"]}


def test_preview_and_configuration_do_not_expose_private_payloads(records, settings):
    settings.KNOWLEDGE_BASE_SEARCH_URL = "https://private-search.example/internal"
    records.run()
    result = analytics.dashboard(records.user)
    assert result["knowledge"]["configured"] is True
    assert result["knowledge"]["documents_count"] is None
    assert result["knowledge"]["memory_status"] == "not_available"
    assert result["workflow_preview"]["nodes"][0]["id"] == "llm"
    serialized = json.dumps(result)
    for secret in (
        "prompt-secret",
        "input-secret",
        "output-secret",
        "config-secret",
        "private-search.example",
    ):
        assert secret not in serialized


def test_billing_summary_selects_current_mode_without_reusing_test_funding(records, settings):
    settings.BILLING_ENABLED = True
    plan = PlanVersion.objects.create(
        code="dashboard",
        version="1",
        name="Dashboard plan",
        price_minor=1000,
        currency="USD",
        included_tokens=1000,
        included_requests=10,
        overage_per_million_tokens_minor=5,
        overage_per_request_minor=1,
    )
    for mode, cap, paid in (("test", 500, True), ("live", 999, False)):
        account = BillingAccount.objects.create(owner=records.user, mode=mode)
        subscription = Subscription.objects.create(
            account=account, plan=plan, status="active", anchor_day=6
        )
        BillingPeriod.objects.create(
            subscription=subscription,
            plan=plan,
            starts_at=NOW - timedelta(days=1),
            ends_at=NOW + timedelta(days=29),
            overage_limit_minor=cap,
            paid_at=NOW if paid else None,
        )
    test = analytics.dashboard(records.user)["billing"]
    assert test["subscription_status"] == "active" and test["overage_limit_minor"] == 500
    settings.BILLING_MODE = "live"
    live = analytics.dashboard(records.user)["billing"]
    assert live["subscription_status"] == "expired" and live["overage_limit_minor"] == 999


def test_lists_are_bounded(records):
    for index in range(10):
        records.run()
        Agent.objects.create(
            owner=records.user,
            name=f"Extra {index}",
            model_name="test-model",
            status="active",
            system_prompt="private",
        )
    result = analytics.dashboard(records.user)
    assert len(result["recent_runs"]) == 8
    assert len(result["active_agents"]) == 5
    assert result["metrics"]["active_runs"] == 10


def test_anonymous_and_inactive_users_cannot_collect_a_dashboard(records):
    with pytest.raises(PermissionDenied):
        analytics.dashboard(AnonymousUser())
    records.user.is_active = False
    with pytest.raises(PermissionDenied):
        analytics.dashboard(records.user)
