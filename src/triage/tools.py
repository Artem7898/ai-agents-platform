"""Trusted save adapter: the model cannot choose identities, keys, or URLs."""

import hmac
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from pydantic import Field, SecretStr

from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolRegistry
from src.runs.models import WorkflowRun
from src.runs.schemas import RunCheckpoint, RunStatus
from src.workflows.schemas import NodeKind, WorkflowSpec

from .schemas import ConfirmedCategoryV1, SinkSubmissionV1, TicketClassificationV1


def sink_token() -> str:
    value = getattr(settings, "DEMO_TRIAGE_SINK_TOKEN", "")
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return value if isinstance(value, str) else ""


def is_valid_sink_token(supplied: str) -> bool:
    expected = sink_token()
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


def configured_sink_url() -> str:
    endpoint = getattr(settings, "DEMO_TRIAGE_SINK_URL", "")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("DEMO_TRIAGE_SINK_URL is not configured")
    try:
        parsed = urlsplit(endpoint)
        # This deliberately local-only demo has no caller-supplied destination.
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/api/demo/triage/sink/"
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The triage sink must use its configured local demo endpoint")
    return endpoint


@sync_to_async(thread_sensitive=True)
def _verify_recorded_human_decision(
    context: ToolExecutionContext,
    ticket_id: str,
    classification: TicketClassificationV1,
    confirmation: ConfirmedCategoryV1,
) -> None:
    """A true boolean alone cannot stand in for a durable human approval."""
    run = WorkflowRun.objects.select_related("workflow").filter(pk=context.run_id).first()
    if (
        run is None
        or context.owner_id is None
        or run.billing_owner_id != context.owner_id
        or run.workflow.owner_id != context.owner_id
        or run.workflow_id != context.workflow_id
        or run.status != RunStatus.RUNNING.value
        or run.input_data.get("ticket_id") != ticket_id
    ):
        raise ValueError("The save context does not identify an owned active ticket run")
    workflow = WorkflowSpec.model_validate(run.workflow_snapshot)
    checkpoint = RunCheckpoint.model_validate(run.checkpoint)
    nodes = {node.id: node for node in workflow.nodes}
    save_node = nodes.get(context.node_id)
    save_tool = next(
        spec for spec in ToolRegistry.get_specs() if spec.name == "save_ticket_classification"
    )
    if (
        save_node is None
        or save_node.kind != NodeKind.TOOL_EXEC
        or save_node.tool_id != save_tool.id
    ):
        raise ValueError("The frozen workflow does not authorize this save tool")
    confirmed = any(
        nodes.get(node_id)
        and nodes[node_id].kind == NodeKind.HITL_APPROVAL
        and node_id in checkpoint.completed
        and ConfirmedCategoryV1.model_validate(decision) == confirmation
        for node_id, decision in checkpoint.human_inputs.items()
    )
    if not confirmed:
        raise ValueError("The save requires the recorded human confirmation")
    classified = any(
        node.kind == NodeKind.LLM_CALL
        and node.output_schema is not None
        and node.output_schema.id == "ticket_classification"
        and node.output_schema.version == 1
        and node.id in checkpoint.completed
        and checkpoint.node_states.get(node.id, {}).get("done") is True
        and TicketClassificationV1.model_validate(checkpoint.node_states[node.id].get("result"))
        == classification
        for node in workflow.nodes
    )
    if not classified:
        raise ValueError("The save requires the run's structured model recommendation")


@ToolRegistry.register(description="Save a ticket category after explicit human confirmation.")
async def save_ticket_classification(
    ticket_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    classification: TicketClassificationV1,
    confirmation: ConfirmedCategoryV1,
    *,
    context: ToolExecutionContext,
) -> dict:
    if not getattr(settings, "DEMO_TRIAGE_ENABLED", False):
        raise ValueError("The ticket triage demo is disabled")
    if context.tool_call_id != "tool":
        raise ValueError("Saving requires the explicit workflow tool node")
    submission = SinkSubmissionV1(
        run_id=context.run_id,
        workflow_id=context.workflow_id,
        node_id=context.node_id,
        idempotency_key=context.idempotency_key,
        ticket_id=ticket_id,
        classification=classification,
        confirmation=confirmation,
    )
    await _verify_recorded_human_decision(context, ticket_id, classification, confirmation)
    endpoint = configured_sink_url()
    token = sink_token()
    if not token:
        raise ValueError("DEMO_TRIAGE_SINK_TOKEN is not configured")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=3.0), trust_env=False
    ) as client:
        response = await client.post(
            endpoint,
            json=submission.model_dump(mode="json"),
            headers={"Idempotency-Key": context.idempotency_key, "X-Demo-Triage-Token": token},
        )
        if response.status_code == 409:
            raise ValueError("The destination rejected a different payload for the same save key")
        response.raise_for_status()
        result = response.json()
    if (
        not isinstance(result, dict)
        or result.get("idempotency_key") != context.idempotency_key
        or result.get("run_id") != str(context.run_id)
        or result.get("ticket_id") != ticket_id
        or result.get("category") != confirmation.category
        or not isinstance(result.get("receipt_id"), str)
    ):
        raise ValueError("The triage sink returned an invalid receipt")
    return result
