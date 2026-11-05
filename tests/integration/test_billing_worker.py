"""Real outbox execution checks paid access immediately before provider dispatch."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from openai import AsyncOpenAI

import src.agents.llm_service as llm_module
import src.billing.entitlements as entitlements
import src.usage.services as usage
from src.billing.models import Invoice, PlanVersion
from src.billing.services import record_payment, start_subscription
from src.runs.management.commands.runworker import executor_handler
from src.runs.models import RunEvent
from src.runs.outbox import OutboxWorker
from src.runs.services import create_run
from src.usage.models import LLMInvocation

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest_asyncio.fixture
async def queued_billing_run(monkeypatch, settings, agent_factory, workflow_factory):
    settings.BILLING_ENABLED = True
    settings.BILLING_MODE = "test"
    received = []

    def respond(request):
        received.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-paid-worker",
                "object": "chat.completion",
                "created": 1,
                "model": "billing-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "subscription answer",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            },
        )

    client = AsyncOpenAI(
        api_key="test-only-worker-key",
        base_url="https://provider.example/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(llm_module, "get_llm_client", lambda: client)

    async def create(*, paid):
        owner = await get_user_model().objects.acreate(username="billing-worker-owner")
        staff = await get_user_model().objects.acreate(
            username="billing-worker-staff", is_staff=True
        )
        plan = await PlanVersion.objects.acreate(
            code="worker",
            version="v1",
            name="Worker plan",
            price_minor=1500,
            currency="USD",
            included_tokens=1_000_000,
            included_requests=100,
            overage_per_million_tokens_minor=100,
            overage_per_request_minor=1,
        )
        period = await sync_to_async(start_subscription)(owner_id=owner.pk, plan_id=plan.pk)
        if paid:
            invoice = await Invoice.objects.aget(period=period, kind="base")
            await sync_to_async(record_payment)(
                invoice_id=invoice.pk,
                actor_id=staff.pk,
                provider="manual",
                merchant_account="worker-fixture",
                external_payment_id="worker-payment",
                amount_minor=invoice.amount_minor,
                currency=invoice.currency,
                evidence_ref="statement:worker-payment",
            )
        agent = await agent_factory(owner=owner, model_name="billing-model")
        workflow = await workflow_factory(
            owner=owner,
            nodes=[{"id": "answer", "kind": "llm_call", "agent_id": str(agent.pk)}],
            edges=[],
            limits={"max_tokens": 10000, "max_output_tokens": 10},
        )
        run = await create_run(workflow.pk, {"prompt": "Hello"}, owner)
        return run, period, received

    yield create
    await client.close()


async def test_confirmed_paid_period_authorizes_the_real_outbox_worker(queued_billing_run):
    run, period, received = await queued_billing_run(paid=True)
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    await period.arefresh_from_db()
    invocation = await LLMInvocation.objects.aget(run=run)
    assert run.status == "completed" and run.output_data["result"] == "subscription answer"
    assert len(received) == 1 and invocation.billing_period_id == period.pk
    assert (period.used_tokens, period.used_calls) == (10, 1)
    assert (period.reserved_tokens, period.reserved_calls) == (0, 0)
    assert invocation.status == "succeeded" and not invocation.reservation_active


@pytest.mark.parametrize("expired", [False, True], ids=["unpaid", "expired-while-queued"])
async def test_worker_rechecks_payment_and_expiry_before_http(
    queued_billing_run, monkeypatch, expired
):
    run, period, received = await queued_billing_run(paid=expired)
    if expired:
        # Freeze admission time only; outbox leases retain their actual clock.
        clock = SimpleNamespace(now=lambda: period.ends_at + timedelta(seconds=1))
        monkeypatch.setattr(usage, "timezone", clock)
        monkeypatch.setattr(entitlements, "timezone", clock, raising=False)
    assert await OutboxWorker.process_next_batch(executor_handler) == 1
    await run.arefresh_from_db()
    await period.arefresh_from_db()
    invocation = await LLMInvocation.objects.aget(run=run)
    assert run.status == "failed" and received == []
    assert invocation.status == "quota_denied" and invocation.error_code.startswith("billing_")
    assert invocation.dispatched_at is None and not invocation.reservation_active
    assert (
        period.used_tokens,
        period.used_calls,
        period.reserved_tokens,
        period.reserved_calls,
    ) == (0, 0, 0, 0)
    assert await RunEvent.objects.filter(run=run, event_type="llm.quota_denied").aexists()
