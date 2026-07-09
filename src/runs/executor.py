import copy
import asyncio
import uuid
from datetime import datetime
from typing import Any
from contextlib import contextmanager
import structlog

from src.agents.llm_service import LLMService
from src.agents.models import Agent
from src.agents.schemas import AgentSpec  # ИСПРАВЛЕНО: Импорт DTO
from src.agents.tool_registry import ToolRegistry
from src.runs.models import WorkflowRun
from src.runs.schemas import RunStatus, NodeKind, EventType

# Безопасный импорт трассировки
try:
    from nova.core.tracing import nova_span as _real_nova_span

    NOVA_TRACING_ENABLED = True
except ImportError:
    NOVA_TRACING_ENABLED = False

log = structlog.get_logger()


@contextmanager
def safe_nova_span(name: str, **kwargs):
    if NOVA_TRACING_ENABLED:
        with _real_nova_span(name, **kwargs) as span:
            yield span
    else:
        log.debug("tracing_disabled", span=name)
        yield None


class WorkflowExecutionError(Exception):
    pass


class WorkflowExecutor:
    def __init__(self, run_id: uuid.UUID):
        self.run_id = run_id
        self.run: WorkflowRun | None = None
        self.context: dict[str, Any] = {}
        self.log = log.bind(run_id=str(run_id))

        # ЭТАП 3: Флаги для реализации State Persistence (HITL)
        self._paused = False
        self._pause_node_id: str | None = None

    async def execute(self) -> None:
        """Точка входа для стандартного запуска."""
        self.run = await WorkflowRun.objects.select_related("workflow").aget(id=self.run_id)
        self.log.info("workflow_execution_started", workflow_id=str(self.run.workflow.id))

        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.RUNNING.value})
        self.run.status = RunStatus.RUNNING.value
        await self.run.asave(update_fields=["status"])

        try:
            async with asyncio.TaskGroup() as tg:
                start_nodes = self._get_start_nodes()
                for node in start_nodes:
                    tg.create_task(self._execute_node(node))

        except* WorkflowExecutionError as eg:
            self.log.error("workflow_execution_failed", exceptions=[str(e) for e in eg.exceptions])
            await self._fail_run(str(eg.exceptions[0]))
            #return  # Прерываем выполнение при ошибке

        except* Exception as eg:
            self.log.exception("unexpected_error_in_taskgroup")
            await self._fail_run("Internal system error")
            #return

        # ==================================================================
        # ЭТАП 3: ПРОВЕРКА НА ПАУЗУ (Вместо слепого статуса COMPLETED)
        # ==================================================================
        if self._paused:
            self.log.info("workflow_paused_for_hitl", node_id=self._pause_node_id)

            # ТЗ: Сохраняем self.context в поле input_data текущего WorkflowRun
            # Используем специальный ключ, чтобы не затереть оригинальный input пользователя
            self.run.input_data["__hitl_state"] = {
                "resume_node_id": self._pause_node_id,
                "context": self.context
            }

            await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.WAITING_HUMAN.value})
            self.run.status = RunStatus.WAITING_HUMAN.value
            await self.run.asave(update_fields=["status", "input_data"])
            return

        # 4. Успешное завершение (только если не было паузы)
        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.COMPLETED.value})
        self.run.status = RunStatus.COMPLETED.value
        self.run.output_data = self.context.get("final_output")
        await self.run.asave(update_fields=["status", "output_data"])
        self.log.info("workflow_execution_completed")

    # ======================================================================
    # ЭТАП 3: МЕТОД ВОЗОБНОВЛЕНИЯ (Вызывается из нового эндпоинта)
    # ======================================================================
    async def resume(self, human_input: dict[str, Any]) -> None:
        """
        ТЗ: POST /api/v2/runs/{run_id}/resume
        Достает контекст из БД и перезапускает граф с нужного места.
        """
        self.run = await WorkflowRun.objects.select_related("workflow").aget(id=self.run_id)

        if self.run.status != RunStatus.WAITING_HUMAN.value:
            raise ValueError(f"Run {self.run_id} is not waiting for human input")

        # 1. Восстанавливаем состояние из "заморозки"
        hitl_state = self.run.input_data.pop("__hitl_state", None)
        if not hitl_state:
            raise ValueError("Corrupted HITL state: cannot find __hitl_state in input_data")

        self._pause_node_id = hitl_state["resume_node_id"]
        self.context = hitl_state["context"]

        # 2. Инжектим ответ человека в контекст графа
        self.context[f"hitl_input_{self._pause_node_id}"] = human_input

        # 3. Убираем мусор из input_data и меняем статус
        await self.run.asave(update_fields=["input_data"])
        await self._append_event(EventType.STATUS_CHANGED,
                                 {"status": RunStatus.RUNNING.value, "reason": "Resumed by human"})
        self.run.status = RunStatus.RUNNING.value
        await self.run.asave(update_fields=["status"])

        # 4. Находим узел, на котором остановились
        resume_node = next((n for n in self.run.workflow.nodes if n["id"] == self._pause_node_id), None)
        if not resume_node:
            await self._fail_run(f"Resume node {self._pause_node_id} not found in workflow")
            return

        self._paused = False  # Сбрасываем флаг

        # 5. Запускаем граф именно с этого узла (и его следующих веток)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._execute_node(resume_node))
        except* Exception as eg:
            await self._fail_run(str(eg.exceptions[0]))
            #return

        if not self._paused:
            await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.COMPLETED.value})
            self.run.status = RunStatus.COMPLETED.value
            self.run.output_data = self.context.get("final_output")
            await self.run.asave(update_fields=["status", "output_data"])

    async def _execute_node(self, node: dict[str, Any]) -> None:
        """Рекурсивный обход DAG."""
        node_id = node["id"]
        node_kind = NodeKind(node["kind"])

        self.log.info("node_started", node_id=node_id, kind=node_kind)
        await self._append_event(EventType.NODE_ENTER, {"node_id": node_id})

        try:
            if node_kind == NodeKind.LLM_CALL:
                await self._handle_llm_call(node)
            elif node_kind == NodeKind.TOOL_EXEC:
                await self._handle_tool_exec(node)
            elif node_kind == NodeKind.HITL_APPROVAL:
                await self._handle_hitl(node)
                # ЭТАП 3: Возвращаемся БЕЗ raise. Ветка графа останавливается.
                return

            await self._append_event(EventType.NODE_EXIT, {"node_id": node_id})

            next_nodes = self._get_next_nodes(node_id)
            if next_nodes:
                async with asyncio.TaskGroup() as tg:
                    for next_node in next_nodes:
                        tg.create_task(self._execute_node(with_context = True))

        except Exception as e:
            self.log.error("node_failed", node_id=node_id, error=str(e))
            await self._append_event(EventType.NODE_EXIT, {"node_id": node_id}, with_context=True)
            # Рекурсивный обход следующих узлов
            next_nodes = self._get_next_nodes(node_id)
            raise WorkflowExecutionError(f"Node {node_id} failed: {e}") from e

    async def _handle_llm_call(self, node: dict[str, Any]) -> None:
        """ИСПРАВЛЕНО: Используем AgentSpec вместо ORM модели (Этап 1 ТЗ)."""
        agent_id = node.get("agent_id")
        agent = await Agent.objects.aget(id=agent_id)
        agent_spec = agent.to_spec()  # Конвертируем в DTO

        llm_service = LLMService()
        tools = ToolRegistry.get_openai_tools_schema()

        prompt = node.get("config", {}).get("prompt_template", "").format(**self.context)

        messages = [
            {"role": "system", "content": agent_spec.system_prompt},
            {"role": "user", "content": prompt}
        ]

        with safe_nova_span("node.llm_agent_loop", node_id=node["id"]):
            while True:
                # ИСПРАВЛЕНО: Передаем agent_spec
                response = await llm_service.generate_with_tools(
                    agent_spec=agent_spec,
                    prompt="",
                    tools=tools if tools else None,
                    context_history=messages
                )

                messages.append(response.model_dump())

                if not response.tool_calls:
                    self.context[f"llm_output_{node['id']}"] = response.content or ""
                    break

                for tool_call in response.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["arguments"]

                    self.log.info("tool_call_dispatched", tool=tool_name, node_id=node["id"])

                    with safe_nova_span("tool.execution", tool=tool_name):
                        result = await ToolRegistry.execute(tool_name, tool_args)

                    self.context[f"tool_result_{tool_call['id']}"] = result

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": str(result)
                    })

    async def _handle_tool_exec(self, node: dict[str, Any]) -> None:
        tool_name = node["config"]["tool_name"]
        arguments = node["config"].get("arguments", {})
        resolved_args = self._resolve_context_vars(arguments)

        with safe_nova_span("node.tool_exec", tool=tool_name):
            result = await ToolRegistry.execute(tool_name, resolved_args)
            self.context[f"tool_result_{node['id']}"] = result

    # ======================================================================
    # ЭТАП 3: ПРАВИЛЬНАЯ РЕАЛИЗАЦИЯ HITL (State Persistence)
    # ======================================================================
    async def _handle_hitl(self, node: dict[str, Any]) -> None:
        """
        ТЗ: Вместо raise мы прерываем текущую ветку графа,
        но другие параллельные ветки продолжат работать.
        """
        self.log.info("waiting_for_human_approval", node_id=node["id"])
        await self._append_event(EventType.HITL_WAITING, {"node_id": node["id"]})

        # Устанавливаем флаги для главного цикла execute()
        self._paused = True
        self._pause_node_id = node["id"]

        # return произойдет автоматически, мягко завершая эту задачу в TaskGroup

    async def _append_event(self, event_type: EventType, payload: dict[str, Any], with_context: bool = False) -> None:
        """
        Event Sourcing: атомарное добавление события в JSONB массив.
        with_context: Если True, делает снимок памяти графа (для Time-Travel UI).
        """
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type.value,
            "payload": payload,
            "timestamp": datetime.utcnow().isoformat()
        }

        # ЭТАП 4: Сохраняем снимок памяти для отладки (Time-Travel)
        if with_context:
            # Делаем глубокую копию, чтобы события не ссылались на один объект в памяти
            event["context_snapshot"] = copy.deepcopy(self.context)

        self.run.events.append(event)
        await self.run.asave(update_fields=["events"])

    async def _fail_run(self, error_msg: str) -> None:
        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.FAILED.value, "error": error_msg})
        self.run.status = RunStatus.FAILED.value
        await self.run.asave(update_fields=["status"])

    def _get_start_nodes(self) -> list[dict]:
        edges = self.run.workflow.edges
        target_ids = {e["target"] for e in edges}
        return [n for n in self.run.workflow.nodes if n["id"] not in target_ids]

    def _get_next_nodes(self, node_id: str) -> list[dict]:
        next_ids = {e["target"] for e in self.run.workflow.edges if e["source"] == node_id}
        return [n for n in self.run.workflow.nodes if n["id"] in next_ids]

    def _resolve_context_vars(self, data: Any) -> Any:
        if isinstance(data, str):
            try:
                return data.format(**self.context)
            except KeyError:
                return data
        elif isinstance(data, dict):
            return {k: self._resolve_context_vars(v) for k, v in data.items()}
        return data