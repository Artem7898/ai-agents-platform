"""Assignment and execution authorization; credentials never enter run snapshots."""

from typing import Any

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError as DjangoValidationError

from src.agents.schemas import AgentSpec

from .models import LLMRuntime
from .policies import validate_runtime_endpoint
from .schemas import RuntimeSpec


def validate_runtime_for_agent(runtime: LLMRuntime, owner_id: Any, agent: AgentSpec) -> RuntimeSpec:
    spec = runtime.to_spec()
    if str(spec.owner_id) != str(owner_id):
        raise ValueError("Runtime must belong to the agent owner")
    if not spec.enabled:
        raise ValueError("Runtime is disabled")
    if agent.model_name not in spec.allowed_models:
        raise ValueError("Model is not allowed by this runtime")
    if agent.tool_ids and not spec.capabilities.tools:
        raise ValueError("Runtime does not support the agent's tools")
    try:
        validate_runtime_endpoint(spec.base_url, spec.profile)
    except DjangoValidationError as exc:
        raise ValueError("Runtime endpoint is not permitted") from exc
    return spec


def snapshot_run_runtimes(agent_snapshots: dict[str, Any], owner_id: Any) -> dict[str, Any]:
    agents = [AgentSpec.model_validate(snapshot["spec"]) for snapshot in agent_snapshots.values()]
    runtime_ids = {agent.runtime_id for agent in agents if agent.runtime_id is not None}
    runtimes = {
        runtime.pk: runtime
        for runtime in LLMRuntime.objects.select_for_update()
        .filter(pk__in=runtime_ids)
        .order_by("pk")
    }
    if len(runtimes) != len(runtime_ids):
        raise ValueError("Agent runtime is unavailable")
    frozen: dict[str, Any] = {}
    for agent in agents:
        if agent.runtime_id is not None:
            spec = validate_runtime_for_agent(runtimes[agent.runtime_id], owner_id, agent)
            frozen[str(agent.runtime_id)] = spec.model_dump(mode="json")
    return frozen


@sync_to_async(thread_sensitive=True)
def get_run_runtime(run: Any, agent: AgentSpec) -> RuntimeSpec | None:
    if agent.runtime_id is None:
        return None
    snapshots = getattr(run, "runtime_snapshots", {})
    data = snapshots.get(str(agent.runtime_id))
    if not data:
        raise ValueError("Run has no immutable snapshot for its runtime")
    frozen = RuntimeSpec.model_validate(data)
    if str(frozen.id) != str(agent.runtime_id):
        raise ValueError("Runtime snapshot identity does not match its agent")
    if str(frozen.owner_id) != str(run.workflow.owner_id):
        raise ValueError("Runtime snapshot does not belong to the workflow owner")
    try:
        current = LLMRuntime.objects.get(pk=agent.runtime_id)
    except LLMRuntime.DoesNotExist as exc:
        raise ValueError("Agent runtime is unavailable") from exc
    # Configuration is frozen, while disabling/reassigning/removing a model is
    # effective immediately for subsequent calls, including resumed runs.
    validate_runtime_for_agent(current, run.workflow.owner_id, agent)
    if not frozen.enabled or agent.model_name not in frozen.allowed_models:
        raise ValueError("Runtime snapshot is unavailable for this model")
    try:
        validate_runtime_endpoint(frozen.base_url, frozen.profile)
    except DjangoValidationError as exc:
        raise ValueError("Runtime snapshot endpoint is not permitted") from exc
    return frozen
