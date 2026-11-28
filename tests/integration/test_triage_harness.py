"""Real PostgreSQL, HTTP sink and SIGKILL; provider content is explicitly mocked."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from src.runs.models import OutboxMessage, WorkflowRun
from src.runs.outbox import _claim
from src.runtimes.models import LLMRuntime
from src.triage.models import TriageReceipt, TriageSinkAttempt
from src.workflows.models import Workflow

pytestmark = pytest.mark.django_db(transaction=True)


def pending_run(owner, name):
    workflow = Workflow.objects.create(
        owner=owner,
        name=name,
        nodes=[
            {
                "id": "start",
                "kind": "condition",
                "config": {"condition": {"path": "input.ok", "op": "exists"}},
            }
        ],
        edges=[],
    )
    run = WorkflowRun.objects.create(workflow=workflow)
    command = OutboxMessage.objects.create(
        run=run, topic="run_workflow", payload={"run_id": str(run.pk)}
    )
    return run, command


def test_explicit_worker_scope_never_claims_another_users_run():
    owner = get_user_model().objects.create_user(username="scope-owner")
    other = get_user_model().objects.create_user(username="scope-other")
    outsider, outsider_command = pending_run(other, "Older unrelated command")
    target, target_command = pending_run(owner, "Explicitly targeted command")
    assert _claim(1, []) == []
    with pytest.raises(ValueError):
        _claim(1, ["not-a-uuid"])
    assert [row.pk for row in _claim(1, [str(target.pk)])] == [target_command.pk]
    outsider.refresh_from_db()
    outsider_command.refresh_from_db()
    assert outsider.execution_token is None
    assert outsider_command.status == "pending" and outsider_command.attempts == 0


def test_failed_capability_probe_leaves_new_runtime_disabled(settings, monkeypatch, tmp_path):
    owner = get_user_model().objects.create_user(username="failed-probe-operator")
    settings.DEMO_TRIAGE_ENABLED = True
    settings.LLM_ALLOWED_BASE_URLS = ["http://127.0.0.1:11435/v1"]

    def failed_probe(*args, **kwargs):
        raise CommandError("Explicitly simulated provider unavailability")

    monkeypatch.setattr("src.triage.management.commands.triage_demo.probe_ollama", failed_probe)
    with pytest.raises(CommandError, match="runtime remains disabled"):
        call_command(
            "triage_demo",
            "seed",
            "--operator",
            owner.username,
            "--base-url",
            "http://127.0.0.1:11435/v1",
            "--model",
            "fixture:1",
            "--output",
            str(tmp_path / "failed-seed"),
        )
    runtime = LLMRuntime.objects.get(owner=owner)
    assert runtime.enabled is False and runtime.capabilities["structured_outputs"] is False
    assert not Workflow.objects.filter(owner=owner).exists()
    failure = json.loads((tmp_path / "failed-seed/probe-failure.json").read_text())
    assert failure["enabled"] is False
    assert failure["runtime_id"] == str(runtime.pk)


@pytest.mark.postgresql
def test_cli_real_postgres_sigkill_replays_original_sink_receipt(tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Subprocess shared-database crash proof requires PostgreSQL")
    owner = get_user_model().objects.create_user(username="mock-proof-operator")
    outsider, outsider_command = pending_run(
        owner, "Unrelated pending command must remain untouched"
    )
    requests = []
    model = "mock-protocol-fixture:1"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/version":
                self.respond({"version": "mock-protocol-fixture"})
            elif self.path == "/api/tags":
                self.respond(
                    {"models": [{"name": model, "model": model, "digest": "sha256:" + "a" * 64}]}
                )
            else:
                self.send_error(404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            assert self.path == "/v1/chat/completions"
            self.respond(
                {
                    "id": "chatcmpl-mock-" + uuid.uuid4().hex,
                    "object": "chat.completion",
                    "created": 1,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "category": "Acceso",
                                        "reason": "Respuesta simulada del protocolo; no es evidencia de inferencia real.",
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = connection.settings_dict
    database_url = "postgresql://{}:{}@{}:{}/{}".format(
        quote(str(config.get("USER", "")), safe=""),
        quote(str(config.get("PASSWORD", "")), safe=""),
        config.get("HOST") or "127.0.0.1",
        config.get("PORT") or 5432,
        quote(str(config["NAME"]), safe=""),
    )
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    env = dict(
        os.environ,
        DJANGO_SETTINGS_MODULE="config.settings.test",
        DATABASE_URL=database_url,
        LLM_ALLOWED_BASE_URLS=json.dumps([base_url]),
        OUTBOX_LEASE_SECONDS="15",
        DEMO_TRIAGE_ENABLED="true",
        BILLING_ENABLED="false",
    )
    seed_output = tmp_path / "mock-seed"
    proof_output = tmp_path / "mock-proof"
    cli = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "triage_demo"]
    try:
        seed = subprocess.run(
            [
                *cli,
                "seed",
                "--operator",
                owner.username,
                "--base-url",
                base_url,
                "--model",
                model,
                "--output",
                str(seed_output),
                "--mock-provider",
                "--timeout",
                "30",
            ],
            env=env,
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert seed.returncode == 0, seed.stdout + seed.stderr
        proof = subprocess.run(
            [
                *cli,
                "proof",
                "--operator",
                owner.username,
                "--scenario",
                str(seed_output / "scenario.json"),
                "--output",
                str(proof_output),
                "--mock-provider",
                "--timeout",
                "30",
            ],
            env=env,
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert proof.returncode == 0, proof.stdout + proof.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    manifest = json.loads((proof_output / "manifest.json").read_text())
    recovery = json.loads((proof_output / "crash/recovery.json").read_text())
    evaluation = json.loads((proof_output / "evaluation.json").read_text())
    assert manifest["metadata"]["mocked"] is True
    assert manifest["metadata"]["provider"] == "mock_ollama_protocol"
    assert evaluation["quality_assessment"]["status"] == "not_run"
    assert evaluation["provider_identity_stable"] is True
    assert manifest["database_vendor"] == "postgresql" and manifest["lease_seconds"] == 15
    assert recovery["returncode"] == -9
    assert recovery["original_receipt"] == recovery["replayed_receipt"]
    assert recovery["database_status_edits"] is False
    assert recovery["lease_wait_seconds"] >= 10
    assert recovery["destination_rows"] == 1 and recovery["outbox_attempts"] == 2
    crash_id = recovery["same_run_id"]
    assert TriageReceipt.objects.filter(run_id=crash_id).count() == 1
    assert list(
        TriageSinkAttempt.objects.filter(receipt__run_id=crash_id).values_list("outcome", flat=True)
    ) == ["created", "replayed"]
    rejection = next(
        row for row in evaluation["cases"] if row["scripted_decision"]["approved"] is False
    )
    assert not TriageReceipt.objects.filter(run_id=rejection["run_id"]).exists()
    correction = evaluation["cases"][0]
    assert correction["recommendation"]["category"] != correction["scripted_decision"]["category"]
    outsider.refresh_from_db()
    outsider_command.refresh_from_db()
    assert outsider.status == "pending" and outsider.execution_token is None
    assert outsider_command.status == "pending" and outsider_command.attempts == 0
    assert len(requests) == 14  # seed probe + proof probe + eight ES + three multilingual + crash
    assert all(request["temperature"] == 0 for request in requests)
    assert all(
        "expected" not in json.dumps(request) and "tools" not in request for request in requests
    )
    assert any("Не могу войти" in json.dumps(request, ensure_ascii=False) for request in requests)
    assert manifest["git"]["sha"] and "source/tracked.patch" in manifest["files_sha256"]
    assert manifest["git"]["unchanged_during_execution"] is True
    assert manifest["git"]["source_at_start"] == manifest["git"]["source_at_export"]
    for relative, expected_hash in manifest["files_sha256"].items():
        assert hashlib.sha256((proof_output / relative).read_bytes()).hexdigest() == expected_hash
    print(f"Explicitly mocked protocol proof artifacts: {proof_output}")
