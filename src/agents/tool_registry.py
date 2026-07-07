import inspect
from typing import Any, Callable, Dict
from nova.core.tracing import nova_span  # ИСПРАВЛЕННЫЙ ИМПОРТ


class ToolRegistry:
    """Реестр инструментов (Function Calling) для LLM."""
    _executors: Dict[str, Callable] = {}
    _definitions: Dict[str, dict] = {}

    @classmethod
    def register(cls, description: str):
        def decorator(func: Callable):
            name = func.__name__
            cls._executors[name] = func

            # Автогенерация JSON Schema для OpenAI на основе сигнатуры функции
            sig = inspect.signature(func)
            properties = {}
            required = []
            for k, v in sig.parameters.items():
                properties[k] = {"type": "string"}  # Упрощенно для тестов
                if v.default == inspect.Parameter.empty:
                    required.append(k)

            cls._definitions[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required
                    }
                }
            }
            return func

        return decorator

    @classmethod
    async def execute(cls, tool_name: str, arguments: Dict[str, Any]) -> Any:
        with nova_span("tool.execute", tool=tool_name):
            if tool_name not in cls._executors:
                raise ValueError(f"Tool '{tool_name}' not found")

            executor = cls._executors[tool_name]
            try:
                return await executor(**arguments)
            except TypeError:
                raise ValueError(f"Invalid arguments for {tool_name}")

    @classmethod
    def get_openai_tools_schema(cls) -> list:
        return list(cls._definitions.values())