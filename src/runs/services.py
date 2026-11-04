"""Atomic run commands. Each transaction stays inside one synchronous boundary."""

import hashlib
import json
import uuid
from typing import Any

from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import Max

from src.agents.models import Agent
from src.agents.schemas import AgentStatus
from src.agents.tool_registry import ToolRegistry
from src.runtimes.access import snapshot_run_runtimes
from src.workflows.models import Workflow
from src.workflows.output_schemas import OutputSchemaRegistry
from src.workflows.schemas import NodeKind, WorkflowSpec

from .models import OutboxMessage, RunEvent, WorkflowRun
from .schemas import EventType, OutboxStatus, ResumeRequest, RunCheckpoint, RunStatus


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Command data must be JSON serializable") from exc


def _key(topic: str, user: Any, key: str) -> str:
    if not 1 <= len(key) <= 128:
        raise ValueError("Idempotency-Key must contain between 1 and 128 characters")
    return topic + ":" + hashlib.sha256(_json([str(user.pk), key]).encode()).hexdigest()


def _require_user(user: Any) -> None:
    if not user.is_authenticated or not user.is_active:
        raise ObjectDoesNotExist("Workflow or run not found")


def _check_visible(run: WorkflowRun, user: Any) -> None:
    if not user.is_staff and run.workflow.owner_id != user.pk:
        raise ObjectDoesNotExist("Workflow or run not found")


def _append_status(run: WorkflowRun, status: str, **payload: Any) -> None:
    sequence = (RunEvent.objects.filter(run=run).aggregate(value=Max("sequence"))["value"] or 0) + 1
    RunEvent.objects.create(
        run=run,
        sequence=sequence,
        event_type=EventType.STATUS_CHANGED.value,
        payload={"status": status, **payload},
    )


def _existing_create(
    command: OutboxMessage, workflow_id: uuid.UUID, input_data: dict[str, Any], user: Any
) -> WorkflowRun:
    run = command.run
    _check_visible(run, user)
    if str(run.workflow_id) != str(workflow_id) or _json(run.input_data) != _json(input_data):
        raise ValueError("Idempotency-Key was already used for different command data")
    return run


def _agent_snapshots(spec: WorkflowSpec, workflow: Workflow) -> dict[str, Any]:
    ids = {node.agent_id for node in spec.nodes if node.agent_id is not None}
    agents = list(Agent.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(agents) != len(ids):
        raise ValueError("Workflow references an unknown agent")
    result: dict[str, Any] = {}
    for agent in agents:
        if getattr(agent, "owner_id") != getattr(workflow, "owner_id"):
            raise ValueError("Workflow and referenced agents must have the same owner")
        if agent.status != AgentStatus.ACTIVE.value:
            raise ValueError("Referenced agents must be active")
        agent_spec = agent.to_spec()
        for tool_id in agent_spec.tool_ids:
            ToolRegistry.get_spec_by_id(tool_id)
        result[str(agent.pk)] = {
            "spec": agent_spec.model_dump(mode="json"),
            "status": agent.status,
            "owner_id": str(getattr(agent, "owner_id"))
            if getattr(agent, "owner_id") is not None
            else None,
        }
    for node in spec.nodes:
        if node.kind == NodeKind.TOOL_EXEC:
            if node.tool_id is None:
                raise ValueError("Tool execution requires tool_id")
            tool = ToolRegistry.get_spec_by_id(node.tool_id)
            configured_name = node.config.get("tool_name")
            if configured_name is not None and configured_name != tool.name:
                raise ValueError("Node tool_id and tool_name must refer to the same tool")
            if node.agent_id is not None:
                allowed = result[str(node.agent_id)]["spec"]["tool_ids"]
                if str(tool.id) not in allowed:
                    raise ValueError("Node tool is not allowed for its referenced agent")
    return result


@sync_to_async(thread_sensitive=True)
def create_run(
    workflow_id: uuid.UUID,
    input_data: dict[str, Any],
    user: Any,
    *,
    idempotency_key: str | None = None,
) -> WorkflowRun:
    _require_user(user)
    input_data = json.loads(_json(input_data))
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be an object")
    dedupe_key = _key("execute", user, idempotency_key) if idempotency_key is not None else None
    with transaction.atomic():
        query = Workflow.objects.select_for_update()
        if not user.is_staff:
            query = query.filter(owner_id=user.pk)
        workflow = query.get(pk=workflow_id)
        if dedupe_key:
            existing = (
                OutboxMessage.objects.select_related("run__workflow")
                .filter(dedupe_key=dedupe_key)
                .first()
            )
            if existing:
                return _existing_create(existing, workflow_id, input_data, user)
        spec = workflow.to_spec()
        output_schemas = OutputSchemaRegistry.snapshot_for_nodes(spec.nodes)
        snapshots = _agent_snapshots(spec, workflow)
        runtimes = snapshot_run_runtimes(snapshots, getattr(workflow, "owner_id"))
        try:
            # A savepoint lets a concurrent unique-key conflict be resolved without
            # leaving a half-created run or poisoning the outer transaction.
            with transaction.atomic():
                run = WorkflowRun.objects.create(
                    workflow=workflow,
                    billing_owner_id=getattr(workflow, "owner_id"),
                    input_data=input_data,
                    workflow_snapshot=spec.model_dump(mode="json"),
                    agent_snapshots=snapshots,
                    runtime_snapshots=runtimes,
                    output_schema_snapshots=output_schemas,
                    checkpoint={},
                    status=RunStatus.PENDING.value,
                )
                OutboxMessage.objects.create(
                    run=run,
                    topic="run_workflow",
                    payload={"run_id": str(run.pk)},
                    dedupe_key=dedupe_key,
                )
                _append_status(run, RunStatus.PENDING.value)
                return run
        except IntegrityError:
            if dedupe_key:
                existing = (
                    OutboxMessage.objects.select_related("run__workflow")
                    .filter(dedupe_key=dedupe_key)
                    .first()
                )
                if existing:
                    return _existing_create(existing, workflow_id, input_data, user)
            raise


def _existing_resume(
    command: OutboxMessage, run_id: uuid.UUID, human_input: dict[str, Any], user: Any
) -> WorkflowRun:
    _check_visible(command.run, user)
    stored = command.payload.get("human_input", {})
    normalized = dict(human_input)
    if "node_id" not in normalized and "node_id" in stored:
        normalized["node_id"] = stored["node_id"]
    if str(command.run.pk) != str(run_id) or _json(stored) != _json(normalized):
        raise ValueError("Idempotency-Key was already used for different command data")
    return command.run


@sync_to_async(thread_sensitive=True)
def enqueue_resume(
    run_id: uuid.UUID, human_input: dict[str, Any], user: Any, *, idempotency_key: str | None = None
) -> WorkflowRun:
    _require_user(user)
    request = ResumeRequest.model_validate(human_input)
    envelope = request.model_dump(mode="json", exclude_none=True)
    _json(envelope)
    supplied_key = _key("resume", user, idempotency_key) if idempotency_key is not None else None
    with transaction.atomic():
        query = WorkflowRun.objects.select_for_update().select_related("workflow")
        if not user.is_staff:
            query = query.filter(workflow__owner_id=user.pk)
        run = query.get(pk=run_id)
        if supplied_key:
            existing = (
                OutboxMessage.objects.select_related("run__workflow")
                .filter(dedupe_key=supplied_key)
                .first()
            )
            if existing:
                return _existing_resume(existing, run_id, envelope, user)
        if run.status != RunStatus.WAITING_HUMAN.value or run.cancel_requested:
            raise ValueError("Run is not waiting for human input")
        checkpoint = RunCheckpoint.model_validate(run.checkpoint)
        if request.node_id is not None:
            if request.node_id not in checkpoint.waiting:
                raise ValueError("Requested node is not waiting for human input")
        elif len(checkpoint.waiting) != 1:
            raise ValueError("node_id is required when multiple approvals are waiting")
        envelope["node_id"] = request.node_id or checkpoint.waiting[0]
        checkpoint_hash = hashlib.sha256(_json(run.checkpoint).encode()).hexdigest()
        # A DAG node is approved once per run. Scope deduplication to that node,
        # so independent approvals remain queueable as other checkpoints advance.
        auto_key = (
            "resume-auto:" + hashlib.sha256(_json([str(run.pk), envelope]).encode()).hexdigest()
        )
        for command in OutboxMessage.objects.filter(
            run=run,
            topic="resume_workflow",
            status__in=[OutboxStatus.PENDING.value, OutboxStatus.PROCESSING.value],
        ):
            stored_input = command.payload.get("human_input", {})
            if stored_input.get("node_id") == envelope["node_id"]:
                if _json(stored_input) == _json(envelope):
                    return run
                raise ValueError("A different resume is already queued for this waiting state")
        try:
            with transaction.atomic():
                OutboxMessage.objects.create(
                    run=run,
                    topic="resume_workflow",
                    dedupe_key=supplied_key or auto_key,
                    payload={
                        "run_id": str(run.pk),
                        "human_input": envelope,
                        "checkpoint_hash": checkpoint_hash,
                    },
                )
        except IntegrityError:
            existing = (
                OutboxMessage.objects.select_related("run__workflow")
                .filter(dedupe_key=supplied_key or auto_key)
                .first()
            )
            if existing:
                return _existing_resume(existing, run_id, envelope, user)
            raise
        return run


@sync_to_async(thread_sensitive=True)
def request_cancel(run_id: uuid.UUID, user: Any) -> WorkflowRun:
    _require_user(user)
    with transaction.atomic():
        query = WorkflowRun.objects.select_for_update().select_related("workflow")
        if not user.is_staff:
            query = query.filter(workflow__owner_id=user.pk)
        run = query.get(pk=run_id)
        if run.status == RunStatus.CANCELLED.value:
            return run
        if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            raise ValueError("A terminal run cannot be cancelled")
        if run.cancel_requested:
            return run
        run.cancel_requested = True
        if run.status in {RunStatus.PENDING.value, RunStatus.WAITING_HUMAN.value}:
            run.status = RunStatus.CANCELLED.value
        run.save(update_fields=["status", "cancel_requested", "updated_at"])
        _append_status(run, run.status, cancel_requested=True)
        return run
