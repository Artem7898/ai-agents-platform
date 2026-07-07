import asyncio
import uuid
from datetime import datetime
from typing import Any
from contextlib import contextmanager
import structlog
from agents.llm_service import LLMService
from src.agents.models import Agent
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

# Безопасная обертка, чтобы executor не падал, если nova.tracing пустой
@contextmanager
def safe_nova_span(name: str, **kwargs):
    if NOVA_TRACING_ENABLED:
        with _real_nova_span(name, **kwargs) as span:
            yield span
    else:
        # Fallback: просто пропускаем, если трассировка не реализована в пакете
        log.debug("tracing_disabled", span=name)
        yield None


class WorkflowExecutionError(Exception):
    """Кастомное исключение для ошибок выполнения DAG."""
    pass


class WorkflowExecutor:
    """
    Асинхронный движок выполнения Workflow (Aggregate Root для Run).
    Использует Event Sourcing для фиксации каждого шага в JSONB.
    """
    def __init__(self, run_id: uuid.UUID):
        self.run_id = run_id
        self.run: WorkflowRun | None = None
        self.context: dict[str, Any] = {}  # Контекст выполнения (передача данных между узлами)
        self.log = log.bind(run_id=str(run_id))

    async def execute(self) -> None:
        """Точка входа. Загружает Run и запускает асинхронный DAG."""
        # 1. Async ORM загрузка
        self.run = await WorkflowRun.objects.select_related("workflow").aget(id=self.run_id)
        self.log.info("workflow_execution_started", workflow_id=str(self.run.workflow.id))

        # 2. Обновляем статус (Event Sourcing)
        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.RUNNING.value})
        self.run.status = RunStatus.RUNNING.value
        await self.run.asave(update_fields=["status"])

        try:
            # 3. Параллельное выполнение графа через TaskGroup
            async with asyncio.TaskGroup() as tg:
                start_nodes = self._get_start_nodes()
                for node in start_nodes:
                    tg.create_task(self._execute_node(node))

        except* WorkflowExecutionError as eg:
            # PEP 654: Обработка группы исключений
            self.log.error("workflow_execution_failed", exceptions=[str(e) for e in eg.exceptions])
            await self._fail_run(str(eg.exceptions[0]))

        except* Exception as eg:
            self.log.exception("unexpected_error_in_taskgroup")
            await self._fail_run("Internal system error")


        # 4. Успешное завершение
        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.COMPLETED.value})
        self.run.status = RunStatus.COMPLETED.value
        self.run.output_data = self.context.get("final_output")
        await self.run.asave(update_fields=["status", "output_data"])
        self.log.info("workflow_execution_completed")

    async def _execute_node(self, node: dict[str, Any]) -> None:
        """Выполняет один узел DAG и рекурсивно запускает следующие."""
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
                return  # Останавливаем граф

            await self._append_event(EventType.NODE_EXIT, {"node_id": node_id})

            # Рекурсивный обход следующих узлов
            next_nodes = self._get_next_nodes(node_id)
            if next_nodes:
                async with asyncio.TaskGroup() as tg:
                    for next_node in next_nodes:
                        tg.create_task(self._execute_node(next_node))

        except Exception as e:
            self.log.error("node_failed", node_id=node_id, error=str(e))
            await self._append_event(EventType.NODE_ERROR, {"node_id": node_id, "error": str(e)})
            raise WorkflowExecutionError(f"Node {node_id} failed: {e}") from e


    async def _handle_llm_call(self, node: dict[str, Any]) -> None:
        """
        Вызов LLM с реализацией Tool Calling (Agent Loop).
        LLM может запросить вызов инструмента несколько раз, прежде чем выдаст финальный ответ.
        """
        # 1. Получаем агента (DDD: передаем целый агрегат, а не ID, чтобы не дергать БД)
        agent_id = node.get("agent_id")
        agent = await Agent.objects.aget(id=agent_id)

        # 2. Подготовка сервиса и инструментов
        llm_service = LLMService()
        tools = ToolRegistry.get_openai_tools_schema()

        # 3. Формируем начальный контекст (System Prompt + User Prompt)
        prompt = node.get("config", {}).get("prompt_template", "").format(**self.context)

        messages = [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": prompt}
        ]

        # 4. AGENT LOOP
        # LLM может захотеть вызвать инструмент, получить результат и продолжить думать.
        with safe_nova_span("node.llm_agent_loop", node_id=node["id"]):
            while True:
                # Вызываем LLM, передавая всю историю переписки
                response = await llm_service.generate_with_tools(
                    agent=agent,
                    prompt="",  # Промпт уже внутри messages
                    tools=tools if tools else None,
                    context_history=messages  # Передаем историю
                )

                # Добавляем ответ LLM в историю (обязательно по спецификации OpenAI для tool calling)
                messages.append(response.model_dump())

                # Если LLM не просит инструменты — выходим из цикла
                if not response.tool_calls:
                    self.context[f"llm_output_{node['id']}"] = response.content or ""
                    break

                # 5. Диспетчеризация инструментов
                for tool_call in response.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["arguments"]

                    self.log.info("tool_call_dispatched", tool=tool_name, node_id=node["id"])

                    # Выполняем инструмент через наш безопасный реестр
                    with safe_nova_span("tool.execution", tool=tool_name):
                        result = await ToolRegistry.execute(tool_name, tool_args)

                    # Сохраняем результат в контекст графа (другие узлы могут его использовать)
                    self.context[f"tool_result_{tool_call['id']}"] = result

                    # 6. Возвращаем результат инструменту в LLM (строго по формату OpenAI)
                    # Если этого не сделать, следующая итерация цикла упадет с ошибкой API
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": str(result)  # LLM ожидает строку
                    })


    async def _handle_tool_exec(self, node: dict[str, Any]) -> None:
        """Прямой вызов инструмента из ToolRegistry."""
        tool_name = node["config"]["tool_name"]
        arguments = node["config"].get("arguments", {})
        resolved_args = self._resolve_context_vars(arguments)

        with safe_nova_span("node.tool_exec", tool=tool_name):
            result = await ToolRegistry.execute(tool_name, resolved_args)
            self.context[f"tool_result_{node['id']}"] = result


    async def _handle_hitl(self, node: dict[str, Any]) -> None:
        """Human-in-the-loop: приостанавливаем workflow."""
        self.log.info("waiting_for_human_approval", node_id=node["id"])
        self.run.status = RunStatus.WAITING_HUMAN.value
        await self.run.asave(update_fields=["status"])
        raise asyncio.CancelledError("Paused for HITL")


    async def _append_event(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """
        Event Sourcing: атомарное добавление события в JSONB массив.
        Не требует отдельной таблицы Event, сохраняется в рамках текущей транзакции Run.
        """
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type.value,
            "payload": payload,
            "timestamp": datetime.utcnow().isoformat()
        }
        # Добавляем в список и сохраняем только это поле (оптимизация Postgres JSONB)
        self.run.events.append(event)
        await self.run.asave(update_fields=["events"])


    async def _fail_run(self, error_msg: str) -> None:
        """Helper для установки статуса FAILED."""
        await self._append_event(EventType.STATUS_CHANGED, {"status": RunStatus.FAILED.value, "error": error_msg})
        self.run.status = RunStatus.FAILED.value
        await self.run.asave(update_fields=["status"])

    # --- Графовые утилиты (заглушки для топологической сортировки) ---

    def _get_start_nodes(self) -> list[dict]:
        """Возвращает узлы, у которых нет входящих ребер."""
        edges = self.run.workflow.edges
        target_ids = {e["target"] for e in edges}
        return [n for n in self.run.workflow.nodes if n["id"] not in target_ids]


    def _get_next_nodes(self, node_id: str) -> list[dict]:
        """Возвращает узлы, куда ведут ребра из текущего."""
        next_ids = {e["target"] for e in self.run.workflow.edges if e["source"] == node_id}
        return [n for n in self.run.workflow.nodes if n["id"] in next_ids]


    def _resolve_context_vars(self, data: Any) -> Any:
        """Подставляет переменные вида {var_name} из self.context."""
        if isinstance(data, str):
            try:
                return data.format(**self.context)
            except KeyError:
                return data
        elif isinstance(data, dict):
            return {k: self._resolve_context_vars(v) for k, v in data.items()}
        return data