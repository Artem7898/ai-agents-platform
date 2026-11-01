"""Scheduler behavior across failures, branches, pauses and recovery."""

import copy
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from src.agents.llm_service import LLMResponse, TokenUsage
from src.agents.schemas import AgentSpec, ToolCall
from src.runs.executor import WorkflowExecutor
from src.workflows.schemas import WorkflowSpec

TOOL_ID = uuid.uuid5(uuid.NAMESPACE_URL, "engine-test-tool")


def tool(node_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "tool_exec",
        "tool_id": str(TOOL_ID),
        "config": {"arguments": arguments or {}},
    }


def graph(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None, **kwargs: Any
) -> WorkflowSpec:
    return WorkflowSpec(name="test", nodes=nodes, edges=edges or [], **kwargs)


class MemoryPersistence:
    def __init__(self, definition: WorkflowSpec, inputs: dict[str, Any] | None = None):
        self.run = SimpleNamespace(
            id=uuid.uuid4(),
            workflow_id=definition.id,
            status="pending",
            input_data=inputs or {},
            checkpoint={},
            workflow_snapshot=definition.model_dump(mode="json"),
            cancel_requested=False,
            workflow=SimpleNamespace(owner_id=None),
            output_data=None,
        )
        self.events: list[dict[str, Any]] = []
        self.statuses: list[str] = []

    async def load_run(self, run_id: uuid.UUID) -> Any:
        assert run_id == self.run.id
        return copy.deepcopy(self.run)

    async def save_progress(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        assert run_id == self.run.id
        self.run.status = kwargs["status"]
        self.run.checkpoint = copy.deepcopy(kwargs["checkpoint"])
        self.run.output_data = copy.deepcopy(kwargs["output_data"])
        self.statuses.append(self.run.status)

    async def append_event(
        self, run_id: uuid.UUID, event_type: str, payload: dict[str, Any], **kwargs: Any
    ) -> None:
        assert run_id == self.run.id
        self.events.append({"type": event_type, "payload": copy.deepcopy(payload), **kwargs})


class Tools:
    def __init__(self, fail: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def get_spec_by_id(self, tool_id: uuid.UUID) -> Any:
        assert tool_id == TOOL_ID
        return SimpleNamespace(id=TOOL_ID, name="echo")

    def get_specs(self, allowed_tool_ids: list[uuid.UUID]) -> list[Any]:
        return [] if TOOL_ID not in allowed_tool_ids else [self.get_spec_by_id(TOOL_ID)]

    async def execute(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        assert name == "echo"
        assert TOOL_ID in kwargs["allowed_tool_ids"]
        assert kwargs["idempotency_key"]
        self.calls.append(copy.deepcopy(arguments))
        if self.fail:
            raise ValueError("deliberate tool failure")
        return arguments.get("value", len(self.calls))


@pytest.mark.parametrize(
    "nodes,edges",
    [
        ([tool("a"), tool("a")], []),
        ([tool("a")], [{"source": "a", "target": "missing"}]),
        ([tool("a"), tool("b")], [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]),
        ([tool("a"), tool("b")], [{"source": "a", "target": "b"}, {"source": "a", "target": "b"}]),
    ],
)
def test_invalid_graph_is_rejected(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValidationError):
        graph(nodes, edges)


def test_python_expression_condition_is_rejected() -> None:
    with pytest.raises(ValidationError):
        graph(
            [tool("a"), tool("b")],
            [{"source": "a", "target": "b", "condition": "__import__('os').system('false')"}],
        )


@pytest.mark.parametrize(
    "node",
    [
        {"id": "a", "kind": "llm_call"},
        {"id": "a", "kind": "tool_exec"},
        {"id": "a", "kind": "condition"},
    ],
)
def test_node_kind_requires_its_configuration(node: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        graph([node])


@pytest.mark.asyncio
async def test_chain_receives_input_and_persists_final_result() -> None:
    spec = graph(
        [tool("a", {"value": "{input.prompt}"}), tool("b", {"value": "{nodes.a.result}"})],
        [{"source": "a", "target": "b"}],
    )
    memory, tools = MemoryPersistence(spec, {"prompt": "hello"}), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert tools.calls == [{"value": "hello"}, {"value": "hello"}]
    assert memory.run.status == "completed"
    assert memory.run.output_data == {"result": "hello", "results": {"b": "hello"}}
    assert memory.run.checkpoint["completed"] == ["a", "b"]


@pytest.mark.asyncio
async def test_diamond_join_executes_once_after_both_predecessors() -> None:
    spec = graph(
        [tool("a"), tool("b"), tool("join", {"value": "{nodes.a.result}"})],
        [{"source": "a", "target": "join"}, {"source": "b", "target": "join"}],
    )
    memory, tools = MemoryPersistence(spec), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    exits = [event["payload"]["node_id"] for event in memory.events if event["type"] == "node_exit"]
    assert exits == ["a", "b", "join"]
    assert len(tools.calls) == 3


@pytest.mark.asyncio
async def test_conditional_branch_skips_inactive_path_and_joins() -> None:
    spec = graph(
        [
            {
                "id": "choose",
                "kind": "condition",
                "config": {"condition": {"path": "input.yes", "op": "eq", "value": True}},
            },
            tool("yes", {"value": "chosen"}),
            tool("no", {"value": "wrong"}),
            tool("join", {"value": "{nodes.yes.result}"}),
        ],
        [
            {"source": "choose", "target": "yes", "condition": True},
            {"source": "choose", "target": "no", "condition": False},
            {"source": "yes", "target": "join"},
            {"source": "no", "target": "join"},
        ],
    )
    memory, tools = MemoryPersistence(spec, {"yes": True}), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "completed"
    assert memory.run.checkpoint["skipped"] == ["no"]
    assert tools.calls == [{"value": "chosen"}, {"value": "chosen"}]


@pytest.mark.asyncio
async def test_failure_remains_failed_and_never_executes_successor() -> None:
    memory = MemoryPersistence(graph([tool("a"), tool("b")], [{"source": "a", "target": "b"}]))
    tools = Tools(fail=True)
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "failed"
    assert "completed" not in memory.statuses
    assert len(tools.calls) == 1
    assert "deliberate tool failure" in memory.run.checkpoint["error"]


@pytest.mark.asyncio
async def test_resume_continues_after_approval_on_new_executor() -> None:
    spec = graph(
        [
            {"id": "approve", "kind": "hitl_approval"},
            tool("next", {"value": "{hitl_input_approve.approved}"}),
        ],
        [{"source": "approve", "target": "next"}],
    )
    memory, tools = MemoryPersistence(spec), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "waiting_human"
    assert not tools.calls
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
        {"input": {"approved": True}}
    )
    assert memory.run.status == "completed"
    assert tools.calls == [{"value": True}]
    assert memory.run.checkpoint["waiting"] == []
    assert memory.run.checkpoint["completed"] == ["approve", "next"]
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
        {"input": {"approved": True}}
    )
    assert tools.calls == [{"value": True}]
    with pytest.raises(ValueError, match="different"):
        await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
            {"input": {"approved": False}}
        )


@pytest.mark.asyncio
async def test_parallel_approvals_preserve_each_waiting_node() -> None:
    spec = graph(
        [
            {"id": "one", "kind": "hitl_approval"},
            {"id": "two", "kind": "hitl_approval"},
            tool("join"),
        ],
        [{"source": "one", "target": "join"}, {"source": "two", "target": "join"}],
    )
    memory, tools = MemoryPersistence(spec), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.checkpoint["waiting"] == ["one", "two"]
    with pytest.raises(ValueError, match="node_id"):
        await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
            {"input": {}}
        )
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
        {"node_id": "one", "input": {"ok": True}}
    )
    assert memory.run.status == "waiting_human"
    assert memory.run.checkpoint["waiting"] == ["two"]
    assert not tools.calls
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(
        {"node_id": "two", "input": {"ok": True}}
    )
    assert memory.run.status == "completed"
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_recovery_skips_completed_nodes_and_uses_snapshot() -> None:
    memory, tools = (
        MemoryPersistence(graph([tool("a"), tool("b")], [{"source": "a", "target": "b"}])),
        Tools(),
    )
    memory.run.checkpoint = {
        "context": {"nodes": {"a": {"result": 99}}},
        "completed": ["a"],
        "executed_steps": 1,
    }
    memory.run.workflow.nodes = []  # Mutable live definition must never be consulted.
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "completed"
    assert len(tools.calls) == 1
    assert memory.run.checkpoint["completed"] == ["a", "b"]


@pytest.mark.asyncio
async def test_node_budget_and_cancellation_have_terminal_states() -> None:
    memory = MemoryPersistence(
        graph([tool("a"), tool("b")], [{"source": "a", "target": "b"}], limits={"max_steps": 1})
    )
    tools = Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "failed"
    assert len(tools.calls) == 1
    memory = MemoryPersistence(graph([tool("a")]))
    memory.run.cancel_requested = True
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "cancelled"


@pytest.mark.asyncio
async def test_llm_loop_preserves_typed_history_and_token_usage() -> None:
    spec = graph([{"id": "chat", "kind": "llm_call", "agent_id": str(uuid.uuid4())}])
    memory, tools = MemoryPersistence(spec, {"prompt": "question"}), Tools()
    agent_spec = AgentSpec(name="test", model_name="fake", system_prompt="test", tool_ids=[TOOL_ID])
    agent = SimpleNamespace(owner_id=None, status="active", to_spec=lambda: agent_spec)

    class LLM:
        calls: list[Any] = []

        async def generate_with_tools(self, **kwargs: Any) -> LLMResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(id="call_1", name="echo", arguments={"value": 42})],
                    usage=TokenUsage(total_tokens=10),
                )
            return LLMResponse(content="answer", usage=TokenUsage(total_tokens=20))

    llm = LLM()
    executor = WorkflowExecutor(
        memory.run.id, persistence=memory, tool_registry=tools, llm_service=llm
    )

    async def load_agent(_agent_id: Any) -> Any:
        return agent

    executor._load_agent = load_agent
    await executor.execute()
    assert memory.run.status == "completed"
    assert memory.run.output_data["result"] == "answer"
    assert memory.run.checkpoint["usage"]["total_tokens"] == 30
    assert llm.calls[0]["context_history"][0].content == "question"
    assert llm.calls[1]["context_history"][-1].tool_call_id == "call_1"
    assert tools.calls == [{"value": 42}]


@pytest.mark.asyncio
async def test_pending_llm_tool_call_is_recovered_without_repeating_provider_call() -> None:
    spec = graph([{"id": "chat", "kind": "llm_call", "agent_id": str(uuid.uuid4())}])
    memory, tools = MemoryPersistence(spec), Tools()
    memory.run.checkpoint = {
        "context": {"nodes": {}},
        "node_states": {
            "chat": {
                "history": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "saved_call",
                                "name": "echo",
                                "arguments": {"value": "recovered"},
                            }
                        ],
                    }
                ],
                "pending_calls": [
                    {"id": "saved_call", "name": "echo", "arguments": {"value": "recovered"}}
                ],
                "next_call": 0,
            }
        },
    }
    agent = SimpleNamespace(
        owner_id=None,
        status="active",
        to_spec=lambda: AgentSpec(
            name="test", model_name="fake", system_prompt="test", tool_ids=[TOOL_ID]
        ),
    )

    class LLM:
        async def generate_with_tools(self, **kwargs: Any) -> LLMResponse:
            assert kwargs["context_history"][-1].tool_call_id == "saved_call"
            return LLMResponse(content="done")

    executor = WorkflowExecutor(
        memory.run.id, persistence=memory, tool_registry=tools, llm_service=LLM()
    )

    async def load_agent(_agent_id: Any) -> Any:
        return agent

    executor._load_agent = load_agent
    await executor.execute()
    assert memory.run.status == "completed"
    assert tools.calls == [{"value": "recovered"}]


@pytest.mark.asyncio
async def test_resume_redelivery_after_decision_checkpoint_continues_without_reapproval() -> None:
    spec = graph(
        [{"id": "approve", "kind": "hitl_approval"}, tool("next")],
        [{"source": "approve", "target": "next"}],
    )
    memory, tools = MemoryPersistence(spec), Tools()
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    first_worker = WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools)

    async def crash_before_continuation() -> None:
        raise RuntimeError("worker process stopped after approval was saved")

    first_worker._run_with_deadline = crash_before_continuation
    command = {"node_id": "approve", "input": {"approved": True}}
    with pytest.raises(RuntimeError, match="worker process"):
        await first_worker.resume(command)
    assert memory.run.status == "running"
    assert memory.run.checkpoint["completed"] == ["approve"]
    assert not tools.calls
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).resume(command)
    assert memory.run.status == "completed"
    assert len(tools.calls) == 1
    assert len([event for event in memory.events if event["type"] == "hitl_resumed"]) == 1


@pytest.mark.asyncio
async def test_recovery_does_not_consume_node_or_tool_budget_twice() -> None:
    spec = graph([tool("only")], limits={"max_steps": 1, "max_tool_calls": 1})
    memory, tools = MemoryPersistence(spec), Tools()
    memory.run.checkpoint = {
        "context": {"nodes": {}},
        "executed_steps": 1,
        "tool_calls": 1,
        "reserved_tool_calls": ["only:tool"],
        "node_states": {"only": {"entered": True}},
    }
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=tools).execute()
    assert memory.run.status == "completed"
    assert memory.run.checkpoint["executed_steps"] == 1
    assert memory.run.checkpoint["tool_calls"] == 1
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_recovered_final_llm_response_cannot_bypass_token_budget() -> None:
    spec = graph(
        [{"id": "chat", "kind": "llm_call", "agent_id": str(uuid.uuid4())}],
        limits={"max_tokens": 10},
    )
    memory = MemoryPersistence(spec)
    memory.run.checkpoint = {
        "context": {"nodes": {}},
        "usage": {"total_tokens": 11},
        "node_states": {"chat": {"entered": True, "done": True, "result": "too expensive"}},
    }
    await WorkflowExecutor(memory.run.id, persistence=memory, tool_registry=Tools()).execute()
    assert memory.run.status == "failed"
    assert "token budget" in memory.run.checkpoint["error"]
