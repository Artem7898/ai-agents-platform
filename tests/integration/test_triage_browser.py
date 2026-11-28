"""Optional Chromium checks for a synthetic, isolated Spanish triage review.

Classification and the waiting checkpoint are fixture data. This test starts no
LLM, worker or destination, and does not claim inference or crash-recovery proof.
"""

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.skipif(
    os.environ.get("CONSOLE_BROWSER_TESTS") != "1",
    reason="Set CONSOLE_BROWSER_TESTS=1 after building browser assets and installing Chromium",
)
PROJECT = Path(__file__).resolve().parents[2]
CLASSIFICATION = {
    "category": "Facturación",
    "reason": "La consulta menciona una factura; esta propuesta es un dato de prueba.",
}
CONFIRMATION = {
    "approved": True,
    "category": "Acceso",
    "note": "El problema es el acceso a la cuenta, no la factura.",
}


@pytest.fixture
def triage_browser_workspace(transactional_db, settings):
    from src.agents.models import Agent
    from src.runs.models import WorkflowRun
    from src.runs.schemas import RunCheckpoint
    from src.workflows.models import Workflow
    from src.workflows.output_schemas import OutputSchemaRegistry

    settings.DEMO_TRIAGE_ENABLED = True
    settings.DEBUG = True
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    settings.SECURE_SSL_REDIRECT = False
    settings.BILLING_ENABLED = False
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "triage-browser-test-cache",
        }
    }
    assert (PROJECT / "src/console/static/console/index.html").is_file(), (
        "Build the frontend before enabling the browser test"
    )
    password = "Triage-browser-" + secrets.token_urlsafe(24)
    owner = get_user_model().objects.create_user(username="triage-browser-owner", password=password)
    other = get_user_model().objects.create_user(username="triage-browser-other", password=password)

    def waiting_fixture(user, ticket_id):
        agent = Agent.objects.create(
            owner=user,
            name="Synthetic triage browser classifier",
            model_name="gpt-4o-mini",
            system_prompt="Synthetic browser fixture; no inference is performed.",
            status="active",
        )
        workflow = Workflow.objects.create(
            owner=user,
            name="Synthetic Spanish triage browser workflow",
            nodes=[
                {
                    "id": "classify",
                    "kind": "llm_call",
                    "agent_id": str(agent.pk),
                    "output_schema": {"id": "ticket_classification", "version": 1},
                },
                {"id": "approval", "kind": "hitl_approval"},
            ],
            edges=[{"source": "classify", "target": "approval"}],
        )
        ticket = {
            "ticket_id": ticket_id,
            "subject": "No puedo consultar mi factura",
            "text": "No puedo iniciar sesión para consultar mi factura. He olvidado mi contraseña.",
        }
        spec = workflow.to_spec()
        checkpoint = RunCheckpoint(
            completed=["classify"],
            waiting=["approval"],
            context={"input": ticket, "nodes": {"classify": {"result": CLASSIFICATION}}},
        )
        return WorkflowRun.objects.create(
            workflow=workflow,
            billing_owner=user,
            status="waiting_human",
            input_data=ticket,
            workflow_snapshot=spec.model_dump(mode="json"),
            agent_snapshots={str(agent.pk): {"spec": agent.to_spec().model_dump(mode="json")}},
            output_schema_snapshots=OutputSchemaRegistry.snapshot_for_nodes(spec.nodes),
            checkpoint=checkpoint.model_dump(mode="json"),
        )

    run = waiting_fixture(owner, "TRIAGE-BROWSER-OWN")
    foreign = waiting_fixture(other, "TRIAGE-BROWSER-PRIVATE")
    return {"username": owner.username, "password": password, "run": run, "foreign": foreign}


@pytest.fixture
def triage_browser_server(triage_browser_workspace, live_server):
    return live_server, triage_browser_workspace


def test_spanish_triage_with_real_browser(triage_browser_server):
    from src.runs.models import OutboxMessage
    from src.triage.models import TriageReceipt, TriageSinkAttempt

    server, workspace = triage_browser_server
    run = workspace["run"]
    checkpoint = run.checkpoint
    assert not OutboxMessage.objects.exists()
    assert not TriageReceipt.objects.exists()
    environment = os.environ.copy()
    environment.update(
        {
            "CONSOLE_E2E_BASE_URL": str(server),
            "CONSOLE_E2E_USERNAME": workspace["username"],
            "CONSOLE_E2E_PASSWORD": workspace["password"],
            "TRIAGE_E2E_RUN_ID": str(run.pk),
        }
    )
    if not environment.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"):
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium:
            environment["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] = chromium
    result = subprocess.run(
        ["npm", "run", "test:e2e", "--", "triage.spec.ts", "--workers=1", "--reporter=line"],
        cwd=PROJECT / "frontend",
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    directory = PROJECT / "frontend/test-results"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "triage-browser.log").write_text(output)
    assert result.returncode == 0, output[-16_000:]

    command = OutboxMessage.objects.get(run=run)
    assert OutboxMessage.objects.count() == 1
    assert command.topic == "resume_workflow" and command.status == "pending"
    assert command.payload["run_id"] == str(run.pk)
    assert command.payload["human_input"] == {"node_id": "approval", "input": CONFIRMATION}
    assert command.attempts == 0
    run.refresh_from_db()
    assert run.status == "waiting_human" and run.checkpoint == checkpoint
    assert run.output_data is None
    assert not OutboxMessage.objects.filter(run=workspace["foreign"]).exists()
    assert not TriageReceipt.objects.exists() and not TriageSinkAttempt.objects.exists()
