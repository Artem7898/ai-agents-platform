"""
LLM Integration Service.
Ответственность: Взаимодействие с OpenAI API (или совместимыми).
Не знает про Django ORM, HTTP запросы или базы данных. Только I/O с LLM.
"""
import json
from typing import AsyncIterator, Type, TypeVar, Any
from contextlib import contextmanager

from pydantic import BaseModel, ValidationError
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
import structlog

# Безопасный импорт трассировки django-nova
try:
    from nova.core.tracing import nova_span as _real_nova_span

    NOVA_TRACING_READY = True
except ImportError:
    NOVA_TRACING_READY = False

from src.agents.models import Agent

log = structlog.get_logger()
T = TypeVar("T", bound=BaseModel)

# ============================================================================
# 1. CONNECTION POOLING (Singleton)
# ============================================================================
# Архитектурное решение: Клиент OpenAI создается один раз на жизненный цикл воркера.
# Это сохраняет TCP-соединения (keep-alive) и исключает开销 на handshake при каждом вызове.
_llm_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        # AsyncOpenAI автоматически читает OPENAI_API_KEY из окружения
        _llm_client = AsyncOpenAI(
            timeout=60.0,  # Жесткий таймаут для защиты от зависания (NFR: resilience)
            max_retries=2  # Одна попытка ретрая при сетевых сбоях
        )
    return _llm_client


# Безопасный контекстный менеджер для трассировки
@contextmanager
def safe_nova_span(name: str, **kwargs):
    if NOVA_TRACING_READY:
        with _real_nova_span(name, **kwargs) as span:
            yield span
    else:
        log.debug("tracing_disabled", span=name)
        yield None


# ============================================================================
# 2. DATA TRANSFER OBJECTS (DTOs)
# ============================================================================
class LLMResponse(BaseModel):
    """Строготипизированный ответ от LLM."""
    content: str | None
    tool_calls: list[dict[str, Any]] = []  # Нормализованный список вызовов инструментов


# ============================================================================
# 3. CORE SERVICE METHODS
# ============================================================================
class LLMService:
    """
    Сервис интеграции с LLM. 
    Содержит бизнес-логику форматирования промптов и парсинга ответов, 
    но не содержит логики оркестрации (это делает WorkflowExecutor).
    """

    @safe_nova_span("llm.generate_with_tools")
    async def generate_with_tools(
            self,
            agent: Agent,  # Передаем Aggregate Root, а не ID (DDD принцип)
            prompt: str,
            tools: list[dict] | None = None,
            context_history: list[dict] | None = None
    ) -> LLMResponse:
        """
        Синхронный вызов LLM с поддержкой Function Calling.
        Используется для узлов DAG, где нам нужен финальный ответ или решение о вызове инструмента.
        """
        client = get_llm_client()

        # Формируем историю сообщений (контекст)
        messages = [{"role": "system", "content": agent.system_prompt}]

        if context_history:
            # Добавляем историю из предыдущих шагов графа (если есть)
            messages.extend(context_history)

        messages.append({"role": "user", "content": prompt})

        try:
            response = await client.chat.completions.create(
                model=agent.model_name,
                messages=messages,
                tools=tools or None,
                temperature=agent.temperature,
            )
        except APITimeoutError:
            log.error("llm_timeout", model=agent.model_name)
            raise  # Пробрасываем дальше, чтобы Executor мог пометить узел как FAILED
        except RateLimitError:
            log.error("llm_rate_limit", model=agent.model_name)
            raise
        except APIError as e:
            log.error("llm_api_error", model=agent.model_name, detail=str(e))
            raise

        choice = response.choices[0]
        tool_calls = []

        # Нормализация tool_calls в удобный для нас формат
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments)
                }
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(
            content=choice.message.content,
            tool_calls=tool_calls
        )

    @safe_nova_span("llm.stream_structured")
    async def stream_and_validate_structured(
            self,
            agent: Agent,
            prompt: str,
            target_schema: Type[T]
    ) -> AsyncIterator[str]:
        """
        Стримит ответ LLM чанками.
        В конце потока проверяет, собрался ли валидный JSON, соответствующий Pydantic схеме.
        Yield'ит только строки (для передачи в SSE Response FastAPI).
        """
        client = get_llm_client()
        buffer = ""

        messages = [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": prompt}
        ]

        try:
            stream = await client.chat.completions.create(
                model=agent.model_name,
                messages=messages,
                stream=True,
                temperature=agent.temperature,
                # Подсказка для LLM генерировать валидный JSON
                response_format={"type": "json_object"}
            )

            async for chunk in stream:
                if not chunk.choices or not chunk.choices[0].delta.content:
                    continue

                delta_content = chunk.choices[0].delta.content
                buffer += delta_content

                # Yield'им сырой текст сразу (для минимальной задержки в UI)
                yield delta_content

        except APIError as e:
            log.error("llm_stream_error", error=str(e))
            yield f"\n[ERROR: LLM Stream Failed: {str(e)}]"
            return

        # --- Post-Stream Validation ---
        # После завершения стрима пробуем распарсить весь буфер как JSON
        try:
            # model_validate_json работает быстрее, чем json.loads + validate
            validated_obj = target_schema.model_validate_json(buffer)
            log.info("structured_output_validated_successfully", schema=target_schema.__name__)


            yield f"\n\n---STRUCTURED_PARSE_SUCCESS---\n{validated_obj.model_dump_json()}\n---END_PARSE---\n"

        except ValidationError as e:
            log.error("structured_output_validation_failed", error=e.errors(), buffer_preview=buffer[:200])
            yield f"\n\n---STRUCTURED_PARSE_ERROR---\n{json.dumps(e.errors())}\n---END_PARSE---\n"
