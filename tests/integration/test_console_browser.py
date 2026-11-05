"""Optional real Chromium checks against an isolated Django live server.

All users, runs, prices and payment records below are synthetic test fixtures.
No worker, inference endpoint or payment processor is started by this test.
"""

import os
import secrets
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

pytestmark = pytest.mark.skipif(
    os.environ.get("CONSOLE_BROWSER_TESTS") != "1",
    reason="Set CONSOLE_BROWSER_TESTS=1 after building browser assets and installing Chromium",
)
PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def console_browser_workspace(transactional_db, settings):
    from src.agents.models import Agent
    from src.billing.models import PlanVersion
    from src.billing.services import record_payment, start_subscription
    from src.runs.models import RunEvent, WorkflowRun
    from src.usage.models import LLMInvocation, OwnerQuota
    from src.workflows.models import Workflow

    settings.DEBUG = True
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    settings.SECURE_SSL_REDIRECT = False
    settings.BILLING_ENABLED = False
    settings.BILLING_MODE = "test"
    settings.KNOWLEDGE_BASE_SEARCH_URL = ""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "console-browser-test-cache",
        }
    }
    assert (PROJECT / "src/console/static/console/index.html").is_file(), (
        "Build the frontend before enabling the browser test"
    )
    password = "Console-browser-" + secrets.token_urlsafe(24)
    user = get_user_model().objects.create_user(
        username="console-browser-owner", password=password, first_name="Artyom"
    )
    staff = get_user_model().objects.create_user(
        username="console-browser-operator", password=password, is_staff=True
    )
    other = get_user_model().objects.create_user(
        username="console-browser-other", password=password
    )
    empty_user = get_user_model().objects.create_user(
        username="console-browser-empty", password=password
    )
    other_agent = Agent.objects.create(
        owner=other,
        name="Other owner's private agent",
        model_name="gpt-4o",
        system_prompt="Private browser fixture",
        status="active",
    )
    agents = [
        Agent.objects.create(
            owner=user, name=name, model_name=model, system_prompt=purpose, status="active"
        )
        for name, model, purpose in (
            ("Research Agent", "gpt-4o", "Research and synthesis"),
            ("Support Agent", "gpt-4o-mini", "Customer support"),
            ("Code Reviewer", "gpt-4o", "Pull request review"),
        )
    ]
    workflow = Workflow.objects.create(
        owner=user,
        name="Research and review",
        nodes=[
            {"id": "research", "kind": "llm_call", "agent_id": str(agents[0].pk)},
            {"id": "approval", "kind": "hitl_approval"},
            {
                "id": "publish",
                "kind": "condition",
                "config": {"condition": {"path": "input.approved", "op": "truthy"}},
            },
        ],
        edges=[
            {"source": "research", "target": "approval"},
            {"source": "approval", "target": "publish"},
        ],
    )
    safe = Workflow.objects.create(
        owner=user,
        name="Browser safe flow",
        nodes=[
            {
                "id": "check",
                "kind": "condition",
                "config": {"condition": {"path": "input.enabled", "op": "truthy"}},
            }
        ],
        edges=[],
    )
    # The reference capture previews the actual multi-node fixture, not the
    # single-node command used later to test harmless queue operations.
    Workflow.objects.filter(pk=workflow.pk).update(updated_at=timezone.now())
    plan = PlanVersion.objects.create(
        code="browser-example",
        version="1",
        name="Browser example plan",
        price_minor=1000,
        currency="USD",
        included_tokens=1_000_000,
        included_requests=1000,
        overage_per_million_tokens_minor=50,
        overage_per_request_minor=0,
    )
    with override_settings(BILLING_ENABLED=True):
        period = start_subscription(owner_id=user.pk, plan_id=plan.pk, overage_limit_minor=200)
        invoice = period.invoices.get(kind="base")
        record_payment(
            invoice_id=invoice.pk,
            actor_id=staff.pk,
            provider="manual",
            merchant_account="browser-fixture",
            external_payment_id="browser-payment-001",
            amount_minor=1000,
            currency="USD",
            evidence_ref="browser-fixture-evidence",
        )
    OwnerQuota.objects.create(owner=user, token_limit=1_000_000, request_limit=1000)
    now = timezone.now()
    waiting_run_id = None
    for index, status in enumerate(("running", "waiting_human", "completed", "failed")):
        started = now - timedelta(hours=index, minutes=12)
        snapshot = workflow.to_spec().model_dump(mode="json")
        agent = agents[index % len(agents)]
        snapshot["nodes"][0]["agent_id"] = str(agent.pk)
        run = WorkflowRun.objects.create(
            workflow=workflow,
            billing_owner=user,
            status=status,
            input_data={"prompt": "Synthetic browser fixture; no real model was called."},
            output_data={"result": "Synthetic completed fixture"}
            if status == "completed"
            else None,
            workflow_snapshot=snapshot,
            agent_snapshots={str(agent.pk): {"spec": agent.to_spec().model_dump(mode="json")}},
            checkpoint={"waiting": ["approval"] if status == "waiting_human" else [], "usage": {}},
        )
        WorkflowRun.objects.filter(pk=run.pk).update(
            created_at=started, updated_at=started + timedelta(minutes=2)
        )
        if status == "waiting_human":
            waiting_run_id = str(run.pk)
        RunEvent.objects.create(
            run=run, sequence=1, event_type="status_changed", payload={"status": status}
        )
        known = status != "running"
        LLMInvocation.objects.create(
            owner=user,
            run=run,
            node_id="research",
            logical_call=1,
            attempt=1,
            model_name=agent.model_name,
            provider="openai",
            operation="completion",
            status="succeeded" if known else "dispatched",
            reserved_tokens=4096,
            max_output_tokens=1024,
            reservation_active=not known,
            prompt_tokens=20_000 * (index + 1) if known else None,
            completion_tokens=10_000 if known else None,
            total_tokens=20_000 * (index + 1) + 10_000 if known else None,
            usage_status="reported" if known else "unknown",
            currency="USD",
            cost=Decimal("0.08") * (index + 1) if known else None,
            started_at=started,
            dispatched_at=started,
            finished_at=started + timedelta(minutes=2) if known else None,
            billing_period=period,
        )
    assert waiting_run_id is not None
    return {
        "username": user.username,
        "password": password,
        "other_agent_id": str(other_agent.pk),
        "safe_workflow_id": str(safe.pk),
        "waiting_run_id": waiting_run_id,
        "empty_username": empty_user.username,
    }


@pytest.fixture
def console_browser_server(console_browser_workspace, live_server):
    return live_server, console_browser_workspace


def test_console_with_real_browser(console_browser_server):
    server, workspace = console_browser_server
    environment = os.environ.copy()
    environment.update(
        {
            "CONSOLE_E2E_BASE_URL": str(server),
            "CONSOLE_E2E_USERNAME": workspace["username"],
            "CONSOLE_E2E_PASSWORD": workspace["password"],
            "CONSOLE_E2E_OTHER_AGENT_ID": workspace["other_agent_id"],
            "CONSOLE_E2E_SAFE_WORKFLOW_ID": workspace["safe_workflow_id"],
            "CONSOLE_E2E_WAITING_RUN_ID": workspace["waiting_run_id"],
            "CONSOLE_E2E_EMPTY_USERNAME": workspace["empty_username"],
        }
    )
    if not environment.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"):
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium:
            environment["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] = chromium
    result = subprocess.run(
        ["npm", "run", "test:e2e", "--", "--workers=1", "--reporter=line"],
        cwd=PROJECT / "frontend",
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    output = result.stdout + result.stderr
    directory = PROJECT / "frontend/test-results"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "console-browser.log").write_text(output)
    assert result.returncode == 0, output[-16_000:]
