"""Repeatable, bounded operator CLI and truthful real-provider crash evidence."""

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone as django_timezone

from src.agents.json_schema import strict_json_schema
from src.agents.models import Agent
from src.runs.models import OutboxMessage, RunEvent, WorkflowRun
from src.runs.services import create_run, enqueue_resume
from src.runtimes.models import LLMRuntime
from src.runtimes.policies import validate_runtime_endpoint
from src.triage.models import TriageReceipt, TriageSinkAttempt
from src.triage.scenario import PROMPT_TEMPLATE, SYSTEM_PROMPT, create_demo_workflow
from src.triage.schemas import CATEGORIES, TicketClassificationV1
from src.workflows.models import Workflow

ROOT = Path(settings.BASE_DIR)
DATA = Path(__file__).resolve().parents[2] / "data"
SMOKE_INPUTS = [
    {
        "ticket_id": "SMOKE-ES",
        "subject": "No puedo entrar",
        "text": "He olvidado mi contraseña y no puedo iniciar sesión.",
    },
    {
        "ticket_id": "SMOKE-EN",
        "subject": "Cannot log in",
        "text": "I forgot my password and cannot sign in to my account.",
    },
    {
        "ticket_id": "SMOKE-RU",
        "subject": "Не могу войти",
        "text": "Я забыл пароль и не могу войти в свой аккаунт.",
    },
]


def _json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def _read(path):
    return json.loads(Path(path).read_text())


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True, timeout=10
    ).stdout


def _untracked_source_files():
    paths = [
        path
        for path in _git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
        if path
    ]
    extensions = {
        ".py",
        ".json",
        ".toml",
        ".lock",
        ".yaml",
        ".yml",
        ".md",
        ".css",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".html",
        ".svg",
        ".ini",
        ".cfg",
    }
    roots = {"src", "tests", "config", "frontend", "deploy", "docs", "scripts"}
    root_files = {
        ".env.example",
        ".gitignore",
        ".dockerignore",
        "Dockerfile",
        "Makefile",
        "LICENSE",
        "README.md",
        "README_RU.md",
        "pyproject.toml",
        "uv.lock",
        "manage.py",
        "compose.yaml",
    }
    for relative in paths:
        path = Path(relative)
        source = (
            path.parts[0] in roots and (path.suffix in extensions or path.name in root_files)
        ) or relative in root_files
        sensitive = (
            (path.name.startswith(".env") and path.name != ".env.example")
            or any(
                part.lower() in {".ssh", ".aws", ".gnupg", "secrets", "credentials", "keys"}
                for part in path.parts
            )
            or bool(
                re.search(
                    r"(?:password|credential|private.?key|access.?token|api.?key)",
                    path.name,
                    flags=re.I,
                )
            )
        )
        if not source or sensitive or (ROOT / relative).is_symlink():
            raise CommandError(
                "Refusing to copy a non-source or credential-like untracked file into evidence; ignore private files before exporting"
            )
    return paths


def _source_state():
    """Hash source bytes before execution and again before evidence is finalized."""
    state = {
        "git_sha": _git("rev-parse", "HEAD").decode().strip(),
        "tracked_patch_sha256": hashlib.sha256(_git("diff", "--binary", "HEAD")).hexdigest(),
        "untracked_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in _untracked_source_files()
        },
    }
    state["fingerprint"] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return state


def _output(path):
    directory = Path(path).resolve()
    if directory == ROOT or directory in ROOT.parents:
        raise CommandError("Evidence must have its own output directory")
    if directory.exists() and any(directory.iterdir()):
        raise CommandError(
            "Choose a fresh, empty evidence directory; existing artifacts are never overwritten"
        )
    if directory.is_relative_to(ROOT):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(directory / "manifest.json")],
            cwd=ROOT,
            timeout=10,
        )
        if ignored.returncode:
            raise CommandError(
                "Evidence inside the repository must use an ignored directory, e.g. deploy/triage/reports/session"
            )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _user(username):
    try:
        return get_user_model().objects.get(username=username, is_active=True)
    except get_user_model().DoesNotExist:
        raise CommandError("Select an existing active operator with --operator") from None


def _scenario(path, user):
    data = _read(path)
    workflow = Workflow.objects.get(pk=data["workflow_id"], owner=user)
    reference = workflow.to_spec().nodes[0].output_schema
    if reference is None or reference.id != "ticket_classification":
        raise CommandError("The scenario is not a ticket triage workflow")
    agent = Agent.objects.get(pk=workflow.to_spec().nodes[0].agent_id, owner=user)
    runtime = LLMRuntime.objects.get(pk=data["runtime_id"], owner=user, enabled=True)
    if (
        agent.runtime_id != runtime.pk
        or agent.model_name != data["model"]
        or runtime.base_url != data["base_url"]
        or runtime.allowed_models != [data["model"]]
        or runtime.profile != "ollama"
        or not runtime.capabilities.get("structured_outputs")
        or not runtime.capabilities.get("temperature")
        or agent.temperature != 0
        or agent.system_prompt != SYSTEM_PROMPT
    ):
        raise CommandError("The current agent/runtime no longer matches the probed scenario")
    return data, workflow


def _fixtures():
    rows = _read(DATA / "tickets_es.json")
    if len(rows) != 8 or any(set(row) != {"ticket_id", "subject", "text"} for row in rows):
        raise CommandError("Expected exactly eight fictional ticket inputs without labels")
    return rows


def ollama_identity(base_url, model, *, timeout):
    """Read the actual version and exact model digest without inferring."""
    endpoint = validate_runtime_endpoint(base_url, "ollama")
    if urlsplit(endpoint).path != "/v1":
        raise CommandError("The Ollama OpenAI-compatible base URL must end in /v1")
    parsed = urlsplit(endpoint)
    native = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=5), follow_redirects=False, trust_env=False
    ) as client:
        version_response = client.get(native + "/api/version")
        version_response.raise_for_status()
        version = version_response.json()
        tags_response = client.get(native + "/api/tags")
        tags_response.raise_for_status()
        tags = tags_response.json()
    if not isinstance(version.get("version"), str) or not version["version"].strip():
        raise CommandError("Ollama must report a nonempty version")
    available = [
        item
        for item in tags.get("models", [])
        if item.get("name") == model or item.get("model") == model
    ]
    if (
        len(available) != 1
        or not isinstance(available[0].get("digest"), str)
        or re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", available[0]["digest"]) is None
    ):
        raise CommandError(
            "The exact model tag and SHA-256 digest must be present in Ollama /api/tags; no model is pulled automatically"
        )
    return {
        "ollama_version": version,
        "model_metadata": available[0],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def probe_ollama(base_url, model, *, timeout, mocked=False):
    """Probe the exact allowlisted endpoint before granting its capability in DB."""
    endpoint = validate_runtime_endpoint(base_url, "ollama")
    identity = ollama_identity(endpoint, model, timeout=timeout)
    schema = strict_json_schema(TicketClassificationV1.model_json_schema())
    probe_input = SMOKE_INPUTS[0]
    request = {
        "model": model,
        "stream": False,
        "max_tokens": 512,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(input=type("Ticket", (), probe_input)()),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "TicketClassificationV1",
                "strict": True,
                "schema": schema,
            },
        },
    }
    started = time.monotonic()
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=5), follow_redirects=False, trust_env=False
    ) as client:
        response = client.post(endpoint + "/chat/completions", json=request)
        response.raise_for_status()
        result = response.json()
    choices = result.get("choices", [])
    if (
        result.get("model") != model
        or len(choices) != 1
        or choices[0].get("finish_reason") != "stop"
    ):
        raise CommandError("The structured capability probe did not finish successfully")
    message = choices[0].get("message", {})
    if message.get("tool_calls") or not isinstance(message.get("content"), str):
        raise CommandError("The structured capability probe returned an invalid message")
    classification = TicketClassificationV1.model_validate_json(
        message["content"], strict=True, extra="forbid"
    )
    return {
        "provider": "mock_ollama_protocol" if mocked else "real_ollama",
        "mocked": mocked,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "base_url": endpoint,
        "model": model,
        **identity,
        "request": request,
        "response": result,
        "validated_output": classification.model_dump(mode="json"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "verified_capabilities": {"structured_outputs": True, "temperature": True},
    }


class Supervisor:
    """Own child handles are the sole authority for signalling a process."""

    def __init__(self, output, *, timeout=300, env=None):
        self.output = Path(output)
        self.timeout = timeout
        self.env = dict(os.environ, **(env or {}))
        self.children = []

    def spawn(self, arguments, name, *, env=None):
        log = self.output / "logs" / (name + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("wb") as stream:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "manage.py"), *arguments],
                cwd=ROOT,
                env=dict(self.env, **(env or {})),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        self.children.append(process)
        return process

    def worker(self, run_id, name):
        return self.spawn(["triage_demo_worker", "--run-id", str(run_id)], name)

    def wait(self, process):
        try:
            code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.stop(process)
            raise CommandError(
                "A child exceeded its bounded timeout; inspect worker logs"
            ) from None
        if code:
            raise CommandError(f"A child exited with status {code}; inspect worker logs")

    def kill_worker(self, process):
        if process not in self.children or process.poll() is not None:
            raise CommandError("Only a live worker spawned by this supervisor may be killed")
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)

    def stop(self, process):
        if process not in self.children:
            raise CommandError("Refusing to signal an unowned process")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for process in reversed(self.children):
            self.stop(process)


def _wait_for(predicate, *, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise CommandError(message)


def _run_record(run):
    run.refresh_from_db()
    return {
        field: getattr(run, field)
        for field in (
            "id",
            "workflow_id",
            "status",
            "input_data",
            "output_data",
            "workflow_snapshot",
            "agent_snapshots",
            "runtime_snapshots",
            "output_schema_snapshots",
            "checkpoint",
            "execution_token",
            "lease_expires_at",
            "created_at",
            "updated_at",
        )
    }


def export_evidence(output, user, run_ids, *, metadata, expected_source=None):
    """Capture immutable definitions, public API event pages and destination audit."""
    from rest_framework.test import APIClient

    output = Path(output)
    client = APIClient()
    client.force_authenticate(user=user)
    for run_id in run_ids:
        run = WorkflowRun.objects.get(pk=run_id, workflow__owner=user)
        folder = output / "runs" / str(run.pk)
        _json(folder / "snapshot.json", _run_record(run))
        _json(folder / "outbox.json", list(OutboxMessage.objects.filter(run=run).values()))
        after, sequences = 0, []
        for page_number in range(1, 1001):
            with override_settings(ALLOWED_HOSTS=[*settings.ALLOWED_HOSTS, "testserver"]):
                response = cast(
                    Any,
                    client.get(f"/console/api/runs/{run.pk}/events/", {"after": after, "limit": 5}),
                )
            if response.status_code != 200:
                raise CommandError(f"Event API evidence failed with HTTP {response.status_code}")
            page = response.json()
            _json(folder / "events" / f"page-{page_number:04}.json", page)
            sequences.extend(item["sequence"] for item in page["events"])
            if not page["has_more"]:
                break
            if page["next_after"] <= after:
                raise CommandError("The event API cursor failed to advance")
            after = page["next_after"]
        expected = list(RunEvent.objects.filter(run=run).values_list("sequence", flat=True))
        if sequences != expected or len(sequences) != len(set(sequences)):
            raise CommandError("Paginated event evidence was incomplete or duplicated")
    receipts = list(TriageReceipt.objects.filter(run_id__in=run_ids).values())
    keys = [row["idempotency_key"] for row in receipts]
    _json(output / "sink-receipts.json", receipts)
    _json(
        output / "sink-attempts.json",
        list(
            TriageSinkAttempt.objects.filter(idempotency_key__in=keys)
            .order_by("created_at")
            .values()
        ),
    )
    source = output / "source"
    source.mkdir(exist_ok=True)
    (source / "tracked.patch").write_bytes(_git("diff", "--binary", "HEAD"))
    untracked = _untracked_source_files()
    # Source paths are explicitly allowed; private configuration must be ignored.
    for relative in untracked:
        path = ROOT / relative
        if path.is_symlink():
            raise CommandError(
                "Cannot make a reproducible evidence snapshot of an untracked symlink"
            )
        destination = source / "untracked" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    state = _git("status", "--porcelain=v1", "--untracked-files=all").decode()
    observed_source = _source_state()
    source_stable = expected_source is None or expected_source == observed_source
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path != output / "manifest.json":
            files[str(path.relative_to(output))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git": {
            "sha": _git("rev-parse", "HEAD").decode().strip(),
            "clean": not state,
            "status": state,
            "tracked_patch": "source/tracked.patch",
            "untracked_files": untracked,
            "source_at_start": expected_source,
            "source_at_export": observed_source,
            "unchanged_during_execution": source_stable if expected_source is not None else None,
        },
        "operator": {"id": user.pk, "username": user.get_username()},
        "database_vendor": connection.vendor,
        "lease_seconds": settings.OUTBOX_LEASE_SECONDS,
        "run_ids": [str(value) for value in run_ids],
        "metadata": metadata,
        "events_transport": "authenticated in-process DRF APIClient, limit=5, checked against append-only rows",
        "files_sha256": files,
    }
    _json(output / "manifest.json", manifest)
    return manifest


class Command(BaseCommand):
    help = "Seed, queue and prove the fictional ticket triage workflow using real Ollama"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        for name in ("seed", "start", "proof", "evidence"):
            item = sub.add_parser(name)
            item.add_argument(
                "--operator", required=True, help="Existing active username; no users are created"
            )
            item.add_argument("--output", required=True, help="Ignored artifact directory")
            item.add_argument(
                "--timeout",
                type=float,
                default=300,
                help="Per-child/provider bound, 10..600 seconds",
            )
            if name != "seed":
                item.add_argument("--scenario", required=name in {"start", "proof"})
            if name == "seed":
                item.add_argument("--base-url", required=True)
                item.add_argument(
                    "--model", required=True, help="Exact already-pulled Ollama model tag"
                )
            if name == "start":
                item.add_argument("--ticket", required=True)
                item.add_argument("--queue-only", action="store_true")
            if name in {"seed", "proof"}:
                item.add_argument(
                    "--mock-provider",
                    action="store_true",
                    help="Label protocol-fixture evidence as mocked",
                )
            if name == "evidence":
                item.add_argument("--run-id", required=True, action="append", type=uuid.UUID)

    def handle(self, *args, **options):
        if not 10 <= options["timeout"] <= 600:
            raise CommandError("Timeout must be between 10 and 600 seconds")
        user = _user(options["operator"])
        output = _output(options["output"])
        action = options["action"]
        if action == "seed":
            self.seed(user, output, options)
        elif action == "start":
            self.start(user, output, options)
        elif action == "proof":
            try:
                self.proof(user, output, options)
            except Exception as exc:
                if not (output / "manifest.json").exists():
                    _json(
                        output / "failure.json",
                        {
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "partial_evidence": True,
                        },
                    )
                raise
        else:
            export_evidence(
                output,
                user,
                options["run_id"],
                metadata={
                    "provider": "see immutable runtime snapshots; no inference performed by evidence command"
                },
            )
            self.stdout.write(str(output / "manifest.json"))

    def seed(self, user, output, options):
        if not getattr(settings, "DEMO_TRIAGE_ENABLED", False):
            raise CommandError("Enable the explicitly local DEMO_TRIAGE_ENABLED setting first")
        runtime = LLMRuntime.objects.create(
            owner=user,
            name="Demo · Ollama ticket triage",
            profile="ollama",
            base_url=options["base_url"],
            allowed_models=[options["model"]],
            enabled=False,
            capabilities={"structured_outputs": False},
        )
        _json(output / "runtime-before-probe.json", runtime.to_spec().model_dump(mode="json"))
        try:
            report = probe_ollama(
                options["base_url"],
                options["model"],
                timeout=options["timeout"],
                mocked=options["mock_provider"],
            )
        except Exception as exc:
            _json(
                output / "probe-failure.json",
                {"runtime_id": str(runtime.pk), "enabled": False, "error_type": type(exc).__name__},
            )
            raise CommandError(
                "Real Ollama structured probe failed; the newly created runtime remains disabled"
            ) from None
        _json(output / "probe.json", report)
        runtime.capabilities = {"structured_outputs": True, "temperature": True}
        runtime.enabled = True
        runtime.save(update_fields=["capabilities", "enabled"])
        workflow = create_demo_workflow(user, runtime, options["model"])
        scenario = {
            "runtime_id": str(runtime.pk),
            "workflow_id": str(workflow.pk),
            "operator_id": user.pk,
            "base_url": runtime.base_url,
            "model": options["model"],
            "mocked": options["mock_provider"],
            "probe": report,
            "runtime": runtime.to_spec().model_dump(mode="json"),
        }
        _json(output / "scenario.json", scenario)
        _json(output / "workflow.json", workflow.to_spec().model_dump(mode="json"))
        self.stdout.write(
            json.dumps(
                {
                    "scenario": str(output / "scenario.json"),
                    "workflow_id": str(workflow.pk),
                    "runtime_id": str(runtime.pk),
                    "provider": report["provider"],
                }
            )
        )

    def start(self, user, output, options):
        _, workflow = _scenario(options["scenario"], user)
        ticket = next((row for row in _fixtures() if row["ticket_id"] == options["ticket"]), None)
        if ticket is None:
            raise CommandError("Unknown fixture ticket ID")
        run = async_to_sync(create_run)(workflow.pk, ticket, user)
        if not options["queue_only"]:
            supervisor = Supervisor(output, timeout=options["timeout"])
            try:
                supervisor.wait(supervisor.worker(run.pk, "classify"))
            finally:
                supervisor.close()
            run.refresh_from_db()
            if run.status != "waiting_human":
                raise CommandError(f"Classification did not reach human review: {run.status}")
        _json(output / "run.json", _run_record(run))
        self.stdout.write(
            json.dumps(
                {
                    "run_id": str(run.pk),
                    "status": run.status,
                    "ui": "/console/triage/",
                    "approval": "No approval has been submitted",
                }
            )
        )

    def proof(self, user, output, options):
        if connection.vendor != "postgresql":
            raise CommandError("The crash proof requires shared PostgreSQL, not SQLite")
        if float(settings.OUTBOX_LEASE_SECONDS) != 15:
            raise CommandError("Set OUTBOX_LEASE_SECONDS=15 before starting the proof")
        if not getattr(settings, "DEMO_TRIAGE_ENABLED", False):
            raise CommandError("Enable the local demo first")
        scenario, workflow = _scenario(options["scenario"], user)
        if bool(scenario.get("mocked", False)) != options["mock_provider"]:
            raise CommandError(
                "Mocked scenarios require --mock-provider; evidence kinds cannot be changed"
            )
        source_at_start = _source_state()
        _json(output / "scenario.json", scenario)
        _json(output / "prompt.json", {"system": SYSTEM_PROMPT, "user_template": PROMPT_TEMPLATE})
        _json(output / "inputs-es.json", _fixtures())
        _json(output / "inputs-smoke.json", SMOKE_INPUTS)
        expected = _read(DATA / "expected_es.json")
        _json(output / "expected-es.json", expected)
        _json(output / "schema.json", TicketClassificationV1.model_json_schema())
        # Re-probe the model currently served; saved capability flags alone are not evidence.
        probe = probe_ollama(
            scenario["base_url"],
            scenario["model"],
            timeout=options["timeout"],
            mocked=options["mock_provider"],
        )
        _json(output / "probe-current.json", probe)
        if probe["model_metadata"]["digest"] != scenario["probe"]["model_metadata"]["digest"]:
            raise CommandError(
                "The Ollama model digest changed since seeding; seed a fresh runtime"
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = uuid.uuid4().hex + uuid.uuid4().hex
        env = {
            "DEMO_TRIAGE_ENABLED": "true",
            "OUTBOX_LEASE_SECONDS": "15",
            "DEMO_TRIAGE_SINK_TOKEN": token,
            "DEMO_TRIAGE_SINK_URL": f"http://127.0.0.1:{port}/api/demo/triage/sink/",
            "DEMO_TRIAGE_COMMIT_BARRIER_SECONDS": "60",
            "ALLOWED_HOSTS": '["localhost","127.0.0.1","testserver"]',
        }
        supervisor = Supervisor(output, timeout=options["timeout"], env=env)
        runs, results = [], []
        # Designate the crash run before starting the sink; its barrier is opt-in and targeted.
        crash_ticket = dict(_fixtures()[0], ticket_id="CRASH-" + uuid.uuid4().hex[:12])
        crash_run = async_to_sync(create_run)(workflow.pk, crash_ticket, user)
        runs.append(crash_run.pk)
        env["DEMO_TRIAGE_COMMIT_BARRIER_RUN_ID"] = str(crash_run.pk)
        sink = supervisor.spawn(["runserver", f"127.0.0.1:{port}", "--noreload"], "sink", env=env)
        try:

            def ready():
                if sink.poll() is not None:
                    raise CommandError("The owned sink server stopped; inspect logs/sink.log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return True
                except OSError:
                    return False

            _wait_for(ready, timeout=20, message="The sink server did not start")
            for index, ticket in enumerate([*_fixtures(), *SMOKE_INPUTS]):
                run = async_to_sync(create_run)(workflow.pk, ticket, user)
                runs.append(run.pk)
                supervisor.wait(supervisor.worker(run.pk, f"{index:02}-classify"))
                run.refresh_from_db()
                if run.status != "waiting_human":
                    raise CommandError(f"Run {run.pk} failed to reach review; status={run.status}")
                recommendation = TicketClassificationV1.model_validate(
                    run.checkpoint["context"]["nodes"]["classify"]["result"]
                ).model_dump()
                # A deliberately rejected decision proves that false never reaches the sink.
                rejected = index == 7
                final_category = (
                    CATEGORIES[(CATEGORIES.index(recommendation["category"]) + 1) % len(CATEGORIES)]
                    if index == 0
                    else recommendation["category"]
                )
                decision = {
                    "approved": not rejected,
                    "category": final_category,
                    "note": "Scripted operator correction for demonstration"
                    if index == 0
                    else "Scripted operator decision",
                }
                async_to_sync(enqueue_resume)(
                    run.pk, {"node_id": "approval", "input": decision}, user
                )
                supervisor.wait(supervisor.worker(run.pk, f"{index:02}-decision"))
                run.refresh_from_db()
                if run.status != "completed":
                    raise CommandError(f"Decision run {run.pk} did not complete")
                count = TriageReceipt.objects.filter(run_id=run.pk).count()
                if count != (0 if rejected else 1):
                    raise CommandError(
                        "The destination receipt count violated the approval condition"
                    )
                if rejected and "save_ticket" not in run.checkpoint.get("skipped", []):
                    raise CommandError("The false approval did not skip the explicit save node")
                results.append(
                    {
                        "run_id": str(run.pk),
                        "ticket_id": ticket["ticket_id"],
                        "recommendation": recommendation,
                        "expected": expected.get(ticket["ticket_id"], "Acceso"),
                        "scripted_decision": decision,
                        "receipt_count": count,
                    }
                )
            supervisor.wait(supervisor.worker(crash_run.pk, "crash-classify"))
            crash_run.refresh_from_db()
            if crash_run.status != "waiting_human":
                raise CommandError("Crash target failed to reach review")
            category = crash_run.checkpoint["context"]["nodes"]["classify"]["result"]["category"]
            decision = {
                "approved": True,
                "category": category,
                "note": "Scripted operator approval for crash proof",
            }
            async_to_sync(enqueue_resume)(
                crash_run.pk, {"node_id": "approval", "input": decision}, user
            )
            worker = supervisor.worker(crash_run.pk, "crash-before-sigkill")

            def committed():
                receipt = TriageReceipt.objects.filter(run_id=crash_run.pk).first()
                if receipt:
                    return receipt
                if worker.poll() is not None:
                    raise CommandError("Crash worker exited before its destination committed")
                return None

            receipt = _wait_for(
                committed,
                timeout=options["timeout"],
                message="No committed destination receipt was observed",
            )
            crash_run.refresh_from_db()
            checkpoint_before = _run_record(crash_run)
            if "save_ticket" in crash_run.checkpoint.get(
                "completed", []
            ) or crash_run.checkpoint.get("node_states", {}).get("save_ticket", {}).get("done"):
                raise CommandError("Fault barrier missed: save was already checkpointed")
            original_receipt = receipt.as_receipt()
            _json(
                output / "crash" / "committed-before-kill.json",
                {"receipt": original_receipt, "run": checkpoint_before, "worker_pid": worker.pid},
            )
            supervisor.kill_worker(worker)
            crash_run.refresh_from_db()
            lease_expiry = crash_run.lease_expires_at
            if lease_expiry is None or lease_expiry <= django_timezone.now():
                raise CommandError("A live 15-second lease was required at SIGKILL")
            killed_at = django_timezone.now()
            # Read-only observation of expiry; no status, token or timestamp writes.
            _wait_for(
                lambda: django_timezone.now() > lease_expiry,
                timeout=20,
                message="The killed worker lease did not expire",
            )
            restarted_at = django_timezone.now()
            supervisor.wait(supervisor.worker(crash_run.pk, "crash-replacement"))
            crash_run.refresh_from_db()
            receipt.refresh_from_db()
            attempts = list(
                TriageSinkAttempt.objects.filter(idempotency_key=receipt.idempotency_key)
                .order_by("created_at")
                .values()
            )
            if (
                crash_run.status != "completed"
                or TriageReceipt.objects.filter(run_id=crash_run.pk).count() != 1
            ):
                raise CommandError(
                    "Crash recovery did not complete the original run with exactly one destination row"
                )
            if (
                receipt.as_receipt() != original_receipt
                or crash_run.output_data["result"] != original_receipt
            ):
                raise CommandError("Replay did not return the original destination receipt")
            if [attempt["outcome"] for attempt in attempts] != ["created", "replayed"]:
                raise CommandError("Expected one committed request and one same-key replay")
            command = OutboxMessage.objects.get(run=crash_run, topic="resume_workflow")
            if command.attempts != 2:
                raise CommandError("The original resume command was not reclaimed exactly once")
            _json(
                output / "crash" / "recovery.json",
                {
                    "same_run_id": str(crash_run.pk),
                    "killed_worker_pid": worker.pid,
                    "signal": "SIGKILL",
                    "returncode": worker.returncode,
                    "killed_at": killed_at,
                    "lease_expires_at": lease_expiry,
                    "replacement_started_at": restarted_at,
                    "lease_wait_seconds": (restarted_at - killed_at).total_seconds(),
                    "original_receipt": original_receipt,
                    "replayed_receipt": crash_run.output_data["result"],
                    "destination_rows": 1,
                    "sink_attempts": attempts,
                    "outbox_attempts": command.attempts,
                    "database_status_edits": False,
                },
            )
            try:
                after_identity = ollama_identity(
                    scenario["base_url"], scenario["model"], timeout=options["timeout"]
                )
                identity_stable = (
                    after_identity["model_metadata"]["digest"] == probe["model_metadata"]["digest"]
                    and after_identity["ollama_version"] == probe["ollama_version"]
                )
            except Exception as exc:
                after_identity = {"error_type": type(exc).__name__}
                identity_stable = False
            _json(output / "provider-identity-after.json", after_identity)
            central_invoice = next(row for row in results if row["ticket_id"] == "ES-001")
            smoke_results = [row for row in results if row["ticket_id"].startswith("SMOKE-")]
            acceptance_checks = {
                "central_invoice_category": central_invoice["recommendation"]["category"]
                == "Facturación",
                "all_three_multilingual_access_categories": len(smoke_results) == 3
                and all(row["recommendation"]["category"] == "Acceso" for row in smoke_results),
            }
            quality = {
                "status": "not_run"
                if probe["mocked"]
                else ("passed" if all(acceptance_checks.values()) else "failed"),
                "reason": "Mock provider supplies fixture responses; real model quality was not assessed"
                if probe["mocked"]
                else "Real inference must classify the central invoice as Facturación and all three ES/EN/RU access smoke tickets as Acceso",
                "checks": {} if probe["mocked"] else acceptance_checks,
            }
            _json(
                output / "evaluation.json",
                {
                    "provider": probe["provider"],
                    "mocked": probe["mocked"],
                    "decisions": "scripted operator; does not claim a person reviewed these tickets",
                    "cases": results,
                    "exact_category_matches": sum(
                        row["recommendation"]["category"] == row["expected"] for row in results
                    ),
                    "total": len(results),
                    "quality_assessment": quality,
                    "provider_identity_stable": identity_stable,
                },
            )
            if not identity_stable or quality["status"] == "failed":
                _json(
                    output / "failure.json",
                    {
                        "status": "failed",
                        "quality_assessment": quality,
                        "provider_identity_stable": identity_stable,
                    },
                )
            # Freeze child logs before hashing evidence; final cleanup is idempotent.
            supervisor.close()
            manifest = export_evidence(
                output,
                user,
                runs,
                metadata={
                    "provider": probe["provider"],
                    "mocked": probe["mocked"],
                    "decisions": "scripted operator",
                    "ollama_version": probe["ollama_version"],
                    "model": scenario["model"],
                    "model_digest": probe["model_metadata"]["digest"],
                    "crash_proof": "crash/recovery.json",
                    "quality_assessment": quality,
                    "provider_identity_stable": identity_stable,
                },
                expected_source=source_at_start,
            )
            if (
                not identity_stable
                or quality["status"] == "failed"
                or not manifest["git"]["unchanged_during_execution"]
            ):
                raise CommandError(
                    f"Proof evidence exported to {output / 'manifest.json'}; real-model acceptance failed, provider identity changed or source changed during execution"
                )
            self.stdout.write(
                json.dumps(
                    {
                        "manifest": str(output / "manifest.json"),
                        "runs": len(manifest["run_ids"]),
                        "crash_run_id": str(crash_run.pk),
                        "provider": probe["provider"],
                        "decisions": "scripted operator",
                    }
                )
            )
        finally:
            supervisor.close()
