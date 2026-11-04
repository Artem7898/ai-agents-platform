"""Durable, deterministic DAG execution.

The worker owns the run lease. This executor saves progress after every node
and before tool dispatch, and passes the fencing token to every write. Nodes
run in topological order; independent branches are currently scheduled
sequentially. Tool backends must honour the supplied idempotency key for
side effects that can be redelivered after a process crash.
"""

import asyncio
import copy
import json
import re
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol

import structlog
from django.db import DatabaseError

from src.agents.llm_service import (
    LLMService,
    StructuredOutputValidationError,
    ToolResponseValidationError,
)
from src.agents.schemas import AgentSpec, ChatMessage, MessageRole, ToolCall
from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolArgumentsValidationError, ToolRegistry
from src.runs.exceptions import CancellationRequested, ExecutionLeaseLost
from src.runs.schemas import EventType, ResumeRequest, RunCheckpoint, RunStatus
from src.workflows.schemas import (
    ConditionPredicate,
    NodeKind,
    WorkflowEdgeSpec,
    WorkflowNodeSpec,
    WorkflowSpec,
)

log = structlog.get_logger()
_MISSING = object()
_TEMPLATE = re.compile(r"\{([\w-]+(?:\.[\w-]+)*)\}")
_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}


class RunPersistence(Protocol):
    def load_run(self, run_id: uuid.UUID) -> Awaitable[Any]: ...

    def append_event(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        context_snapshot: dict[str, Any] | None = None,
        *,
        execution_token: uuid.UUID | None = None,
    ) -> Awaitable[Any]: ...

    def save_progress(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        checkpoint: dict[str, Any],
        output_data: dict[str, Any] | None = None,
        execution_token: uuid.UUID | None = None,
    ) -> Awaitable[Any]: ...


class WorkflowExecutionError(Exception):
    """A deterministic workflow/configuration/budget error."""


class RunCancelled(Exception):
    """Internal control flow for an explicitly cancelled run."""


def lookup_path(context: dict[str, Any], path: str) -> Any:
    """Traverse dictionaries/lists only; no attribute access or evaluation."""
    value: Any = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def evaluate_condition(predicate: ConditionPredicate, context: dict[str, Any]) -> bool:
    actual = lookup_path(context, predicate.path)
    if predicate.op == "exists":
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    if predicate.op == "truthy":
        return bool(actual)
    if predicate.op == "falsy":
        return not bool(actual)
    if predicate.op == "eq":
        return actual == predicate.value
    if predicate.op == "ne":
        return actual != predicate.value
    if predicate.op == "in":
        return actual in predicate.value
    if predicate.op == "not_in":
        return actual not in predicate.value
    try:
        if predicate.op == "gt":
            return actual > predicate.value
        if predicate.op == "gte":
            return actual >= predicate.value
        if predicate.op == "lt":
            return actual < predicate.value
        return actual <= predicate.value
    except TypeError as error:
        raise WorkflowExecutionError("Condition compared incompatible value types") from error


class WorkflowExecutor:
    def __init__(
        self,
        run_id: uuid.UUID,
        *,
        execution_token: uuid.UUID | None = None,
        persistence: RunPersistence | None = None,
        llm_service: LLMService | None = None,
        tool_registry: Any = ToolRegistry,
    ):
        self._metered = persistence is None
        if persistence is None:
            from src.runs import persistence as persistence_module

            persistence = persistence_module
        self.run_id = run_id
        self.execution_token = execution_token
        assert persistence is not None
        self.persistence: RunPersistence = persistence
        self.llm_service = llm_service or LLMService()
        self.registry = tool_registry
        self.run: Any = None
        self.workflow: WorkflowSpec | None = None
        self.checkpoint = RunCheckpoint()
        self.context: dict[str, Any] = {}
        self.log = log.bind(run_id=str(run_id))
        self._timer = time.monotonic()
        self._elapsed_base = 0.0

    async def execute(self) -> None:
        self.run = await self.persistence.load_run(self.run_id)
        if self.run.status in _TERMINAL:
            return
        try:
            self._restore()
            if self.run.status == RunStatus.WAITING_HUMAN.value:
                return
            await self._run_with_deadline()
        except (RunCancelled, CancellationRequested):
            await self._finish(RunStatus.CANCELLED)
        except (WorkflowExecutionError, ValueError, KeyError, TypeError) as error:
            await self._fail_run(str(error))

    async def resume(self, human_input: dict[str, Any]) -> None:
        self.run = await self.persistence.load_run(self.run_id)
        self._restore()
        if "input" in human_input or "node_id" in human_input:
            command = ResumeRequest.model_validate(human_input)
        else:
            command = ResumeRequest(input=human_input)
        node_id = command.node_id
        if node_id is None:
            if len(self.checkpoint.waiting) == 1:
                node_id = self.checkpoint.waiting[0]
            elif not self.checkpoint.waiting and len(self.checkpoint.human_inputs) == 1:
                # Compatibility for a direct single-approval retry. Durable
                # commands always freeze their resolved node_id at enqueue.
                node_id = next(iter(self.checkpoint.human_inputs))
            else:
                raise ValueError("node_id is required when several approvals are waiting")
        already_approved = node_id in self.checkpoint.human_inputs
        if already_approved:
            if self.checkpoint.human_inputs[node_id] != command.input:
                raise ValueError("This approval already has a different recorded decision")
            if node_id not in self.checkpoint.completed:
                raise ValueError("Corrupted checkpoint: approval decision is not completed")
            if self.run.status in _TERMINAL:
                return
        elif self.run.status in _TERMINAL:
            raise ValueError(f"Run {self.run_id} is already terminal")
        elif node_id not in self.checkpoint.waiting:
            raise ValueError("The requested node is not waiting for human input")
        try:
            await self._check_cancelled()
            if not already_approved:
                self.checkpoint.waiting.remove(node_id)
                self.checkpoint.human_inputs[node_id] = copy.deepcopy(command.input)
                self.context[f"hitl_input_{node_id}"] = copy.deepcopy(command.input)
                self._set_result(node_id, command.input)
                self.checkpoint.completed.append(node_id)
                await self._persist(RunStatus.RUNNING)
                await self._event(EventType.HITL_RESUMED, {"node_id": node_id})
                await self._event(EventType.NODE_EXIT, {"node_id": node_id}, snapshot=True)
            await self._run_with_deadline()
        except (RunCancelled, CancellationRequested):
            await self._finish(RunStatus.CANCELLED)
        except (WorkflowExecutionError, ValueError, KeyError, TypeError) as error:
            await self._fail_run(str(error))

    def _restore(self) -> None:
        snapshot = self.run.workflow_snapshot
        if not snapshot:
            raise WorkflowExecutionError("Run has no immutable workflow snapshot")
        # Existing runs retain their frozen fail-fast behavior; only newly
        # created snapshots opt into the new repair default.
        snapshot = copy.deepcopy(snapshot)
        snapshot.setdefault("limits", {}).setdefault("max_tool_repairs", 0)
        snapshot["limits"].setdefault("max_output_repairs", 0)
        self.workflow = WorkflowSpec.model_validate(snapshot)
        if self.workflow.id != self.run.workflow_id:
            raise WorkflowExecutionError("Snapshot does not match the run workflow")
        if self.run.checkpoint:
            self.checkpoint = RunCheckpoint.model_validate(self.run.checkpoint)
        else:
            inputs = copy.deepcopy(self.run.input_data)
            self.checkpoint = RunCheckpoint(
                context={**inputs, "input": inputs, "nodes": {}},
                started_at=datetime.now(UTC).isoformat(),
            )
        known = {node.id for node in self.workflow.nodes}
        completed = set(self.checkpoint.completed)
        skipped = set(self.checkpoint.skipped)
        waiting = set(self.checkpoint.waiting)
        if not (completed | skipped | waiting) <= known:
            raise WorkflowExecutionError("Checkpoint references unknown workflow nodes")
        if completed & skipped or completed & waiting or skipped & waiting:
            raise WorkflowExecutionError("Checkpoint contains conflicting node states")
        if self.checkpoint.executed_steps > self.workflow.limits.max_steps:
            raise WorkflowExecutionError("Run node budget exceeded")
        if self.checkpoint.tool_calls > self.workflow.limits.max_tool_calls:
            raise WorkflowExecutionError("Run tool-call budget exceeded")
        if (
            not self._metered
            and self.checkpoint.usage.get(
                "budget_tokens", self.checkpoint.usage.get("total_tokens", 0)
            )
            > self.workflow.limits.max_tokens
        ):
            raise WorkflowExecutionError("Run token budget exceeded")
        self.context = self.checkpoint.context
        self._elapsed_base = self.checkpoint.elapsed_seconds
        self._timer = time.monotonic()

    async def _run_with_deadline(self) -> None:
        assert self.workflow is not None
        remaining = self.workflow.limits.max_duration_seconds - self._elapsed_base
        if remaining <= 0:
            raise WorkflowExecutionError("Run time budget exceeded")
        try:
            async with asyncio.timeout(remaining):
                await self._persist(RunStatus.RUNNING)
                if (
                    self.checkpoint.usage.get(
                        "budget_tokens", self.checkpoint.usage.get("total_tokens", 0)
                    )
                    > self.workflow.limits.max_tokens
                ):
                    raise WorkflowExecutionError("Run token budget exceeded")
                await self._drive()
        except TimeoutError as error:
            raise WorkflowExecutionError("Run time budget exceeded") from error

    async def _drive(self) -> None:
        assert self.workflow is not None
        incoming: dict[str, list[WorkflowEdgeSpec]] = {node.id: [] for node in self.workflow.nodes}
        for edge in self.workflow.edges:
            incoming[edge.target].append(edge)
        while True:
            progressed = False
            terminal = set(self.checkpoint.completed) | set(self.checkpoint.skipped)
            for node in self.workflow.nodes:
                if node.id in terminal or node.id in self.checkpoint.waiting:
                    continue
                predecessors = incoming[node.id]
                if any(edge.source not in terminal for edge in predecessors):
                    continue
                await self._check_cancelled()
                if predecessors and not any(self._edge_enabled(edge) for edge in predecessors):
                    self.checkpoint.skipped.append(node.id)
                    terminal.add(node.id)
                    await self._persist(RunStatus.RUNNING)
                    await self._event(EventType.NODE_SKIPPED, {"node_id": node.id})
                    progressed = True
                    continue
                await self._execute_node(node)
                terminal = set(self.checkpoint.completed) | set(self.checkpoint.skipped)
                progressed = True
            if not progressed:
                break
        if self.checkpoint.waiting:
            await self._finish(RunStatus.WAITING_HUMAN)
            return
        unresolved = {node.id for node in self.workflow.nodes} - terminal
        if unresolved:
            raise WorkflowExecutionError("Graph cannot progress from its saved checkpoint")
        await self._finish(RunStatus.COMPLETED, self._output())

    def _edge_enabled(self, edge: WorkflowEdgeSpec) -> bool:
        if edge.source in self.checkpoint.skipped:
            return False
        if edge.condition is None:
            return True
        if isinstance(edge.condition, bool):
            return self.context["nodes"][edge.source]["result"] is edge.condition
        return evaluate_condition(edge.condition, self.context)

    async def _execute_node(self, node: WorkflowNodeSpec) -> None:
        assert self.workflow is not None
        state = self.checkpoint.node_states.setdefault(node.id, {})
        if not state.get("entered"):
            if self.checkpoint.executed_steps >= self.workflow.limits.max_steps:
                raise WorkflowExecutionError("Run node budget exceeded")
            self.checkpoint.executed_steps += 1
            state["entered"] = True
            await self._persist(RunStatus.RUNNING)
            await self._event(EventType.NODE_ENTER, {"node_id": node.id, "kind": node.kind.value})
        try:
            if node.kind == NodeKind.HITL_APPROVAL:
                self.checkpoint.waiting.append(node.id)
                await self._persist(RunStatus.RUNNING)
                await self._event(EventType.HITL_WAITING, {"node_id": node.id})
                return
            if node.kind == NodeKind.LLM_CALL:
                result = await self._handle_llm_call(node)
                self.context[f"llm_output_{node.id}"] = result
            elif node.kind == NodeKind.TOOL_EXEC:
                result = await self._handle_tool_exec(node)
                self.context[f"tool_result_{node.id}"] = result
            else:
                predicate = ConditionPredicate.model_validate(node.config["condition"])
                result = evaluate_condition(predicate, self.context)
            self._set_result(node.id, result)
            self.checkpoint.completed.append(node.id)
            await self._persist(RunStatus.RUNNING)
            await self._event(EventType.NODE_EXIT, {"node_id": node.id}, snapshot=True)
        except (
            asyncio.CancelledError,
            RunCancelled,
            CancellationRequested,
            ExecutionLeaseLost,
            DatabaseError,
        ):
            raise
        except Exception as error:
            await self._event(EventType.NODE_FAILED, {"node_id": node.id, "error": str(error)})
            raise WorkflowExecutionError(f"Node {node.id} failed: {error}") from error

    async def _handle_llm_call(self, node: WorkflowNodeSpec) -> str | dict[str, Any]:
        assert self.workflow is not None
        agent = await self._load_agent(node.agent_id)
        agent_spec = agent.to_spec()
        if str(agent.owner_id) != str(self.run.workflow.owner_id):
            raise WorkflowExecutionError("Agent does not belong to the workflow owner")
        if agent.status != "active":
            raise WorkflowExecutionError("LLM nodes require an active agent")
        if node.output_schema is not None:
            return await self._handle_structured_llm_call(node, agent_spec)
        allowed_ids = agent_spec.tool_ids
        tools = self.registry.get_specs(allowed_tool_ids=allowed_ids)
        state = self.checkpoint.node_states.setdefault(node.id, {})
        if state.get("done"):
            return str(state["result"])
        if "history" not in state:
            prompt = self._resolve_context_vars(
                node.config.get("prompt_template", "{input.prompt}")
            )
            state.update(
                history=[
                    ChatMessage(role=MessageRole.USER, content=str(prompt)).model_dump(mode="json")
                ],
                pending_calls=[],
                next_call=0,
            )
        while True:
            await self._check_cancelled()
            if state["next_call"] < len(state["pending_calls"]):
                pending = [ToolCall.model_validate(call) for call in state["pending_calls"]]
                try:
                    self.registry.validate_calls(pending, allowed_tool_ids=allowed_ids)
                except ToolArgumentsValidationError as error:
                    if state["next_call"] or any(
                        f"{node.id}:{call.id}" in self.checkpoint.reserved_tool_calls
                        for call in pending
                    ):
                        raise WorkflowExecutionError(
                            "A partially executed tool batch no longer passes validation"
                        ) from None
                    # A restored batch has its assistant message persisted, but
                    # no tool has run. Remove it before asking for a correction.
                    last = state["history"][-1] if state["history"] else {}
                    if (
                        last.get("role") != "assistant"
                        or last.get("tool_calls") != state["pending_calls"]
                    ):
                        raise WorkflowExecutionError("Saved tool batch has inconsistent history")
                    state["history"].pop()
                    await self._repair_tool_response(node, state, error.feedback, error.issues)
                    continue
                self._preflight_tool_budget(node, pending)
            for index in range(state["next_call"], len(state["pending_calls"])):
                call = state["pending_calls"][index]
                await self._reserve_tool_call(f"{node.id}:{call['id']}")
                result = await self.registry.execute(
                    call["name"],
                    call["arguments"],
                    allowed_tool_ids=allowed_ids,
                    timeout=self.workflow.limits.tool_timeout_seconds,
                    idempotency_key=f"{self.run_id}:{node.id}:{call['id']}",
                    execution_context=self._tool_context(node.id, call["id"]),
                )
                state["history"].append(
                    ChatMessage(
                        role=MessageRole.TOOL, content=json.dumps(result), tool_call_id=call["id"]
                    ).model_dump(mode="json")
                )
                state["next_call"] = index + 1
                await self._persist(RunStatus.RUNNING)
                await self._event(
                    EventType.TOOL_CALL,
                    {"node_id": node.id, "tool": call["name"], "tool_call_id": call["id"]},
                )
            await self._refresh_usage()
            remaining_tokens = self.workflow.limits.max_tokens - self.checkpoint.usage.get(
                "budget_tokens", self.checkpoint.usage.get("total_tokens", 0)
            )
            if remaining_tokens <= 0:
                raise WorkflowExecutionError("Run token budget exceeded")
            if (
                state.get("rounds", 0)
                >= self.workflow.limits.max_tool_calls + self.workflow.limits.max_tool_repairs + 1
            ):
                raise WorkflowExecutionError("Run LLM-round budget exceeded")
            runtime_kwargs: dict[str, Any] = {}
            if self._metered:
                from src.usage.contracts import InvocationContext

                runtime_kwargs["invocation_context"] = InvocationContext(
                    run_id=self.run_id,
                    node_id=node.id,
                    logical_call=state.get("rounds", 0),
                    execution_token=self.execution_token,
                )
            if agent_spec.runtime_id is not None:
                from src.runtimes.access import get_run_runtime

                runtime_kwargs["runtime"] = await get_run_runtime(self.run, agent_spec)
            try:
                response = await self.llm_service.generate_with_tools(
                    agent_spec=agent_spec,
                    prompt="",
                    tools=tools,
                    context_history=[
                        ChatMessage.model_validate(message) for message in state["history"]
                    ],
                    max_output_tokens=min(self.workflow.limits.max_output_tokens, remaining_tokens),
                    **runtime_kwargs,
                )
            except ToolResponseValidationError as error:
                self._account_response(error, remaining_tokens)
                state["rounds"] = state.get("rounds", 0) + 1
                await self._repair_tool_response(
                    node,
                    state,
                    "Return tool arguments as a JSON object matching the provided schema.",
                    ({"code": error.code, "path": ["arguments"]},),
                )
                continue
            self._account_response(response, remaining_tokens)
            state["rounds"] = state.get("rounds", 0) + 1
            accepted_ids = {
                call["id"] for message in state["history"] for call in message.get("tool_calls", [])
            }
            if any(call.id in accepted_ids for call in response.tool_calls):
                raise WorkflowExecutionError("Provider reused a tool call identifier")
            try:
                self.registry.validate_calls(response.tool_calls, allowed_tool_ids=allowed_ids)
            except ToolArgumentsValidationError as error:
                await self._repair_tool_response(node, state, error.feedback, error.issues)
                continue
            self._preflight_tool_budget(node, response.tool_calls)
            state["history"].append(response.as_message().model_dump(mode="json"))
            state["pending_calls"] = [call.model_dump(mode="json") for call in response.tool_calls]
            state["next_call"] = 0
            if not response.tool_calls:
                state.update(done=True, result=response.content or "")
            await self._persist(RunStatus.RUNNING)
            if self.checkpoint.usage["budget_tokens"] > self.workflow.limits.max_tokens:
                raise WorkflowExecutionError("Run token budget exceeded")
            if state.get("done"):
                return str(state["result"])

    async def _handle_structured_llm_call(
        self, node: WorkflowNodeSpec, agent_spec: AgentSpec
    ) -> dict[str, Any]:
        """Produce one schema-bound object, with no tool dispatch or text fallback."""
        from src.workflows.output_schemas import OutputSchemaRegistry

        assert self.workflow is not None
        target_schema = OutputSchemaRegistry.resolve_snapshot(
            node, getattr(self.run, "output_schema_snapshots", {})
        )
        state = self.checkpoint.node_states.setdefault(node.id, {})
        if state.get("pending_calls"):
            raise WorkflowExecutionError("Structured output nodes cannot restore pending tools")
        if state.get("done"):
            # A crash can leave a validated result saved just before NODE_EXIT.
            # Preserve its object type and never dispatch another provider call.
            return self._saved_structured_result(state.get("result"), target_schema)
        if "history" not in state:
            prompt = self._resolve_context_vars(
                node.config.get("prompt_template", "{input.prompt}")
            )
            state["history"] = [
                ChatMessage(role=MessageRole.USER, content=str(prompt)).model_dump(mode="json")
            ]
        while True:
            await self._check_cancelled()
            await self._refresh_usage()
            remaining_tokens = self.workflow.limits.max_tokens - self.checkpoint.usage.get(
                "budget_tokens", self.checkpoint.usage.get("total_tokens", 0)
            )
            if remaining_tokens <= 0:
                raise WorkflowExecutionError("Run token budget exceeded")
            if state.get("output_rounds", 0) >= self.workflow.limits.max_output_repairs + 1:
                raise WorkflowExecutionError("Structured output round budget exceeded")
            runtime_kwargs: dict[str, Any] = {}
            if self._metered:
                from src.usage.contracts import InvocationContext

                runtime_kwargs["invocation_context"] = InvocationContext(
                    run_id=self.run_id,
                    node_id=node.id,
                    logical_call=state.get("output_rounds", 0),
                    execution_token=self.execution_token,
                )
            if agent_spec.runtime_id is not None:
                from src.runtimes.access import get_run_runtime

                runtime_kwargs["runtime"] = await get_run_runtime(self.run, agent_spec)
            # Re-check the live registered contract before every request. A
            # queued correction cannot silently follow a changed schema version.
            target_schema = OutputSchemaRegistry.resolve_snapshot(
                node, getattr(self.run, "output_schema_snapshots", {})
            )
            try:
                response = await self.llm_service.generate_structured_response(
                    agent_spec=agent_spec,
                    prompt="",
                    target_schema=target_schema,
                    context_history=[
                        ChatMessage.model_validate(message) for message in state["history"]
                    ],
                    max_output_tokens=min(self.workflow.limits.max_output_tokens, remaining_tokens),
                    **runtime_kwargs,
                )
            except StructuredOutputValidationError as error:
                self._account_response(error, remaining_tokens)
                state["output_rounds"] = state.get("output_rounds", 0) + 1
                await self._repair_structured_response(node, state, error.feedback, error.issues)
                continue
            self._account_response(response, remaining_tokens)
            state["output_rounds"] = state.get("output_rounds", 0) + 1
            result = self._saved_structured_result(response.data, target_schema)
            state.update(done=True, result=result)
            await self._persist(RunStatus.RUNNING)
            if self.checkpoint.usage["budget_tokens"] > self.workflow.limits.max_tokens:
                raise WorkflowExecutionError("Run token budget exceeded")
            return result

    @staticmethod
    def _saved_structured_result(value: Any, target_schema: Any) -> dict[str, Any]:
        """Validate saved JSON without exposing Pydantic's rejected input text."""
        if not isinstance(value, dict):
            raise WorkflowExecutionError("Saved structured result must be a JSON object")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 256_000:
                raise ValueError("Result size limit")
            target_schema.model_validate_json(encoded, strict=True, extra="forbid")
        except Exception:
            raise WorkflowExecutionError(
                "Saved structured result does not match its frozen contract"
            ) from None
        return copy.deepcopy(value)

    async def _repair_structured_response(
        self,
        node: WorkflowNodeSpec,
        state: dict[str, Any],
        feedback: str,
        issues: tuple[dict[str, Any], ...],
    ) -> None:
        assert self.workflow is not None
        repairs = state.get("output_repairs", 0)
        can_retry = repairs < self.workflow.limits.max_output_repairs
        if can_retry:
            state["output_repairs"] = repairs + 1
            state["history"].append(
                ChatMessage(
                    role=MessageRole.USER,
                    content=(
                        "The previous structured response failed output validation. "
                        + feedback
                        + " Return a complete JSON object matching the requested output schema."
                    ),
                ).model_dump(mode="json")
            )
        # Persist the granted correction before any further billable request.
        await self._persist(RunStatus.RUNNING)
        await self._event(
            EventType.OUTPUT_VALIDATION_FAILED,
            {
                "node_id": node.id,
                "repair_count": state.get("output_repairs", 0),
                "will_retry": can_retry,
                "issues": list(issues),
            },
        )
        if not can_retry:
            raise WorkflowExecutionError("Structured output repair budget exhausted")

    def _account_response(self, response: Any, remaining_tokens: int) -> None:
        if self._metered:
            return
        reported_tokens = response.usage.total_tokens
        accounted_tokens = response.accounted_tokens
        if accounted_tokens is None:
            # Injected services can omit accounting metadata; absent usage
            # consumes the remaining local budget instead of fabricating zero.
            accounted_tokens = reported_tokens if reported_tokens is not None else remaining_tokens
        budget_before = self.checkpoint.usage.get(
            "budget_tokens", self.checkpoint.usage.get("total_tokens", 0)
        )
        self.checkpoint.usage["budget_tokens"] = budget_before + accounted_tokens
        self.checkpoint.usage["total_tokens"] = self.checkpoint.usage.get("total_tokens", 0) + (
            reported_tokens or 0
        )
        self.checkpoint.usage["unknown_calls"] = (
            self.checkpoint.usage.get("unknown_calls", 0)
            + response.unknown_calls
            + int(reported_tokens is None and response.accounted_tokens is None)
        )

    def _preflight_tool_budget(self, node: WorkflowNodeSpec, calls: list[ToolCall]) -> None:
        assert self.workflow is not None
        needed = sum(
            f"{node.id}:{call.id}" not in self.checkpoint.reserved_tool_calls for call in calls
        )
        if self.checkpoint.tool_calls + needed > self.workflow.limits.max_tool_calls:
            raise WorkflowExecutionError("Run tool-call budget exceeded")

    async def _repair_tool_response(
        self,
        node: WorkflowNodeSpec,
        state: dict[str, Any],
        feedback: str,
        issues: tuple[dict[str, Any], ...],
    ) -> None:
        assert self.workflow is not None
        repairs = state.get("repairs", 0)
        can_retry = repairs < self.workflow.limits.max_tool_repairs
        state["pending_calls"] = []
        state["next_call"] = 0
        if can_retry:
            state["repairs"] = repairs + 1
            state["history"].append(
                ChatMessage(
                    role=MessageRole.USER,
                    content=(
                        "The previous tool-call response was rejected before execution. "
                        + feedback
                        + " Regenerate the complete response using only the available tools."
                    ),
                ).model_dump(mode="json")
            )
        # Save the correction and its counter before another billable request.
        await self._persist(RunStatus.RUNNING)
        await self._event(
            EventType.TOOL_VALIDATION_FAILED,
            {
                "node_id": node.id,
                "repair_count": state.get("repairs", 0),
                "will_retry": can_retry,
                "issues": list(issues),
            },
        )
        if not can_retry:
            raise WorkflowExecutionError("Tool argument repair budget exhausted")

    def _tool_context(self, node_id: str, call_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            run_id=self.run_id,
            workflow_id=self.run.workflow_id,
            node_id=node_id,
            tool_call_id=call_id,
            owner_id=getattr(self.run, "billing_owner_id", None),
            idempotency_key=f"{self.run_id}:{node_id}:{call_id}",
        )

    async def _load_agent(self, agent_id: uuid.UUID | None) -> Any:
        snapshot = self.run.agent_snapshots.get(str(agent_id))
        if not snapshot:
            raise WorkflowExecutionError("Run has no immutable snapshot for its agent")
        spec = AgentSpec.model_validate(snapshot["spec"])
        return SimpleNamespace(
            owner_id=snapshot["owner_id"], status=snapshot["status"], to_spec=lambda: spec
        )

    async def _reserve_tool_call(self, call_key: str) -> None:
        assert self.workflow is not None
        if call_key in self.checkpoint.reserved_tool_calls:
            return
        if self.checkpoint.tool_calls >= self.workflow.limits.max_tool_calls:
            raise WorkflowExecutionError("Run tool-call budget exceeded")
        self.checkpoint.tool_calls += 1
        self.checkpoint.reserved_tool_calls.append(call_key)
        await self._persist(RunStatus.RUNNING)

    async def _handle_tool_exec(self, node: WorkflowNodeSpec) -> Any:
        assert self.workflow is not None
        state = self.checkpoint.node_states.setdefault(node.id, {})
        if state.get("done"):
            return state["result"]
        spec = self.registry.get_spec_by_id(node.tool_id)
        arguments = self._resolve_context_vars(node.config.get("arguments", {}))
        if not isinstance(arguments, dict):
            raise WorkflowExecutionError("Tool arguments must resolve to an object")
        await self._reserve_tool_call(f"{node.id}:tool")
        result = await self.registry.execute(
            spec.name,
            arguments,
            allowed_tool_ids=[node.tool_id],
            timeout=self.workflow.limits.tool_timeout_seconds,
            idempotency_key=f"{self.run_id}:{node.id}:tool",
            execution_context=self._tool_context(node.id, "tool"),
        )
        state.update(done=True, result=result)
        await self._persist(RunStatus.RUNNING)
        return result

    def _resolve_context_vars(self, data: Any) -> Any:
        if isinstance(data, str):
            whole = _TEMPLATE.fullmatch(data)
            if whole:
                value = lookup_path(self.context, whole.group(1))
                if value is _MISSING:
                    raise WorkflowExecutionError(f"Missing template input: {whole.group(1)}")
                return copy.deepcopy(value)

            def replace(match: re.Match[str]) -> str:
                value = lookup_path(self.context, match.group(1))
                if value is _MISSING:
                    raise WorkflowExecutionError(f"Missing template input: {match.group(1)}")
                return str(value)

            return _TEMPLATE.sub(replace, data)
        if isinstance(data, dict):
            return {key: self._resolve_context_vars(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self._resolve_context_vars(value) for value in data]
        return data

    def _set_result(self, node_id: str, result: Any) -> None:
        json.dumps(result)  # Reject a non-serializable tool result before checkpointing.
        self.context.setdefault("nodes", {})[node_id] = {"result": copy.deepcopy(result)}

    def _output(self) -> dict[str, Any]:
        assert self.workflow is not None
        if self.workflow.output_node_id:
            result = (
                self.context.get("nodes", {}).get(self.workflow.output_node_id, {}).get("result")
            )
            return {"result": copy.deepcopy(result)}
        sources = {edge.source for edge in self.workflow.edges}
        results = {
            node.id: copy.deepcopy(self.context["nodes"][node.id]["result"])
            for node in self.workflow.nodes
            if node.id not in sources and node.id in self.checkpoint.completed
        }
        return {"result": next(reversed(results.values())) if results else None, "results": results}

    async def _check_cancelled(self) -> None:
        current = await self.persistence.load_run(self.run_id)
        if current.cancel_requested or current.status == RunStatus.CANCELLED.value:
            raise RunCancelled()

    def _checkpoint_data(self) -> dict[str, Any]:
        self.checkpoint.elapsed_seconds = self._elapsed_base + time.monotonic() - self._timer
        return self.checkpoint.model_dump(mode="json")

    async def _refresh_usage(self) -> None:
        if not self._metered:
            return
        from src.usage.reporting import run_usage

        totals = await run_usage(self.run_id)
        usage = self.checkpoint.usage
        # Freeze pre-ledger consumption before the first metered dispatch. The
        # baseline is persisted with the running checkpoint, then combined with
        # ledger totals on every refresh, never incremented a second time.
        for field in ("budget_tokens", "total_tokens", "unknown_calls"):
            baseline = {
                "budget_tokens": "ledger_base_tokens",
                "total_tokens": "ledger_base_reported_tokens",
                "unknown_calls": "ledger_base_unknown_calls",
            }[field]
            if baseline not in usage:
                usage[baseline] = (
                    usage.get(
                        field, usage.get("total_tokens", 0) if field == "budget_tokens" else 0
                    )
                    if totals["calls"] == 0
                    else 0
                )
            usage[field] = usage[baseline] + totals[field]

    async def _persist(self, status: RunStatus, output: dict[str, Any] | None = None) -> None:
        await self._refresh_usage()
        await self.persistence.save_progress(
            self.run_id,
            status=status.value,
            checkpoint=self._checkpoint_data(),
            output_data=output,
            execution_token=self.execution_token,
        )
        self.run.status = status.value

    async def _event(
        self, event_type: EventType, payload: dict[str, Any], *, snapshot: bool = False
    ) -> None:
        await self.persistence.append_event(
            self.run_id,
            event_type.value,
            payload,
            context_snapshot=copy.deepcopy(self.context) if snapshot else None,
            execution_token=self.execution_token,
        )

    async def _finish(self, status: RunStatus, output: dict[str, Any] | None = None) -> None:
        await self._persist(status, output)

    async def _fail_run(self, error_msg: str) -> None:
        self.log.error("workflow_execution_failed", error=error_msg)
        self.checkpoint.error = error_msg
        try:
            await self._persist(RunStatus.FAILED)
        except CancellationRequested:
            await self._finish(RunStatus.CANCELLED)
