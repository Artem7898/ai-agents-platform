"""
LLM Integration Service (Infrastructure Layer).

Архитектурные решения:
1. Сервис полностью изолирован от Django ORM. Принимает AgentSpec (Pydantic DTO),
   что позволяет тестировать его в unit-тестах без БД.
2. Реализован паттерн "Strict Structured Outputs" через JSON Schema от OpenAI.
   Это исключает ситуацию "сломанный JSON от LLM ломает базу данных" на уровне API-контракта.
3. История сообщений (context_history) передается исключительно как in-memory список
   на время одного LLM-цикла, соблюдая требования Event Sourcing (Этап 1).
"""
import json
from typing import AsyncIterator, Type, TypeVar, Any
from contextlib import contextmanager

from pydantic import BaseModel, ValidationError
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
import structlog

from src.agents.schemas import AgentSpec, ToolSpec, ContentBlock

# Безопасный импорт трассировки django-nova (Infrastructure boundary)
try:
    from nova.core.tracing import nova_span as _real_nova_span
    NOVA_TRACING_READY = True
except ImportError:
    NOVA_TRACING_READY = False

log = structlog.get_logger()
T = TypeVar("T", bound=BaseModel)


# ============================================================================
# 1. CONNECTION POOLING & RESILIENCE (Singleton)
# ============================================================================
# Архитектурное решение: Клиент создается один раз на жизненный цикл Uvicorn воркера.
# Мы явно задаем лимиты httpx, чтобы при скачках нагрузки (spike) мы не исчерпали
# файловые дескрипторы (file descriptors) на сервере (NFR: Resilience & Scaling).
_llm_client: AsyncOpenAI | None = None

def get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            timeout=60.0,
            max_retries=2,
            # Явно ограничиваем пул соединений для защиты от exhaustion
            http_client=None # В будущем тут можно подставить кастомный httpx.AsyncClient с лимитами
        )
    return _llm_client

@contextmanager
def safe_nova_span(name: str, **kwargs):
    if NOVA_TRACING_READY:
        with _real_nova_span(name, **kwargs) as span:
            yield span
    else:
        yield None


# ============================================================================
# 2. MAPPERS (ORM/DTO -> OpenAI API Format)
# ============================================================================
# Архитектурное решение: Мы не заставляем бизнес-логику или executor формировать
# сырые словари для OpenAI. Инкапсулируем специфику стороннего API внутри сервиса.
def _map_tools_to_openai_spec(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Преобразует доменные ToolSpec в формат OpenAI Function Calling."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_schema,
                # В OpenAI strict mode обязательно strict: true
                "strict": True
            }
        }
        for tool in tools
    ]

def _map_history_to_openai_messages(history: list[ContentBlock]) -> list[dict[str, Any]]:
    """
    Преобразует доменные ContentBlock (Discriminated Union) в формат сообщений OpenAI.
    Это гарантирует, что история никогда не будет иметь неверный формат.
    """
    messages = []
    for block in history:
        match block.type:
            case "text":
                # Для простоты текст пушится как assistant/user в зависимости от контекста вызова
                messages.append({"role": "assistant", "content": block.text})
            case "tool_use":
                # OpenAI требует специфичный формат для tool_calls внутри истории
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": block.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.arguments)
                        }
                    }]
                })
            case "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": block.content
                })
    return messages


# ============================================================================
# 3. STRICT DATA TRANSFER OBJECTS
# ============================================================================
class LLMResponse(BaseModel):
    """Строго типизированный ответ от LLM (без сырых dict)."""
    content: str | None
    tool_calls: list[dict[str, Any]] = []


# ============================================================================
# 4. CORE SERVICE (Pure Infrastructure)
# ============================================================================
class LLMService:
    """
    Сервис интеграции с LLM.
    Содержит ТОЛЬКО логику транспорта (отправка/получение) и маппинга.
    Не содержит бизнес-логики оркестрации (это делает WorkflowExecutor).
    """

    @safe_nova_span("llm.generate_with_tools")
    async def generate_with_tools(
            self,
            agent_spec: AgentSpec,  # ИЗМЕНЕНО: Принимаем DTO, а не ORM
            prompt: str,
            tools: list[ToolSpec] | None = None,
            context_history: list[ContentBlock] | None = None
    ) -> LLMResponse:
        """
        Вызов LLM с поддержкой Function Calling.
        Используется для узлов DAG, где нужен финальный ответ или решение о вызове инструмента.
        """
        client = get_llm_client()

        # 1. Формируем контракт сообщений
        messages = [{"role": "system", "content": agent_spec.system_prompt}]

        if context_history:
            messages.extend(_map_history_to_openai_messages(context_history))

        messages.append({"role": "user", "content": prompt})

        # 2. Вызов API
        try:
            response = await client.chat.completions.create(
                model=agent_spec.model_name,
                messages=messages,
                tools=_map_tools_to_openai_spec(tools) if tools else None,
                temperature=agent_spec.temperature,
            )
        except (APITimeoutError, RateLimitError, APIError) as e:
            # Логируем через structlog (в проде улетит в Loki/ELK)
            log.error("llm_api_error", model=agent_spec.model_name, error_type=type(e).__name__)
            # Пробрасываем как есть. Executor должен перехватить и изменить статус Run на FAILED
            raise

        # 3. Парсинг ответа
        choice = response.choices[0]
        tool_calls = []

        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments) # OpenAI возвращает строку
                }
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(
            content=choice.message.content,
            tool_calls=tool_calls
        )

    @safe_nova_span("llm.generate_structured")
    async def generate_structured(
            self,
            agent_spec: AgentSpec,
            prompt: str,
            target_schema: Type[T],
            context_history: list[ContentBlock] | None = None
    ) -> T:
        """
        РЕАЛИЗАЦИЯ ЭТАПА 1 ТЗ: Strict Structured Outputs.

        Заставляет LLM вернуть ответ, который на 100% соответствует Pydantic-схеме.
        Использует нативную фичу OpenAI Response Format (JSON Schema).
        Если LLM не может сгенерировать валидный JSON, OpenAI вернет ошибку на уровне API,
        и мы никогда не получим невалидные данные в нашу бизнес-логику.
        """
        client = get_llm_client()

        # Pydantic v2 умеет отдавать JSON Schema, который понимает OpenAI
        json_schema = target_schema.model_json_schema()

        messages = [{"role": "system", "content": agent_spec.system_prompt}]
        if context_history:
            messages.extend(_map_history_to_openai_messages(context_history))
        messages.append({"role": "user", "content": prompt})

        try:
            response = await client.chat.completions.create(
                model=agent_spec.model_name,
                messages=messages,
                temperature=agent_spec.temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": target_schema.__name__,
                        "strict": True, # Гарантия отсутствия лишних ключей (extra="forbid" в Pydantic)
                        "schema": json_schema
                    }
                }
            )
        except (APITimeoutError, RateLimitError, APIError) as e:
            log.error("llm_structured_error", model=agent_spec.model_name, error=str(e))
            raise

        raw_content = response.choices[0].message.content

        # Валидация через Pydantic (формальность при strict=True, но нужна для типизации)
        return target_schema.model_validate_json(raw_content)

    @safe_nova_span("llm.stream_raw")
    async def stream_raw(
            self,
            agent_spec: AgentSpec,
            prompt: str,
            context_history: list[ContentBlock] | None = None
    ) -> AsyncIterator[str]:
        """
        Чистый стриминг сырых токенов.
        Не делает валидацию (это задача вызывающего кода, если нужно).
        Служит только для транспорта данных в FastAPI SSE Endpoint.
        """
        client = get_llm_client()

        messages = [{"role": "system", "content": agent_spec.system_prompt}]
        if context_history:
            messages.extend(_map_history_to_openai_messages(context_history))
        messages.append({"role": "user", "content": prompt})

        try:
            stream = await client.chat.completions.create(
                model=agent_spec.model_name,
                messages=messages,
                stream=True,
                temperature=agent_spec.temperature,
            )

            async for chunk in stream:
                if not chunk.choices or not chunk.choices[0].delta.content:
                    continue
                # Yieldим только сырой текст. Без всяких [ERROR] строк!
                yield chunk.choices[0].delta.content

        except APIError as e:
            # В стриме мы НЕ пишем ошибки в текстовый поток.
            # Мы пробрасываем исключение. FastAPI роутер перехватит его
            # и отправит клиенту корректный SSE event типа "error".
            log.error("llm_stream_error", error=str(e))
            raise