import pytest
from src.agents.tool_registry import ToolRegistry


class TestToolRegistry:
    """Тесты реестра инструментов (Async Tool Registry)."""

    def test_register_tool(self):
        """Регистрация инструмента через декоратор."""
        @ToolRegistry.register(description="Test tool")
        async def test_tool(query: str, limit: int = 5) -> str:
            return f"Result for {query}"

        assert "test_tool" in ToolRegistry._executors
        assert "test_tool" in ToolRegistry._definitions
        # ИСПРАВЛЕНО: обращаемся к словарю по ключам, как в реальном OpenAI JSON Schema
        assert ToolRegistry._definitions["test_tool"]["function"]["description"] == "Test tool"

    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        """Успешное выполнение зарегистрированного инструмента."""
        @ToolRegistry.register(description="Add numbers")
        async def add_numbers(a: int, b: int) -> int:
            return a + b

        result = await ToolRegistry.execute("add_numbers", {"a": 5, "b": 3})
        assert result == 8

    @pytest.mark.asyncio
    async def test_execute_tool_validation_error(self):
        """Ошибка при вызове инструмента без обязательных аргументов."""

        @ToolRegistry.register(description="Multiply")
        async def multiply(x: int, y: int) -> int:
            return x * y

        # ИСПРАВЛЕНО: Передаем только один аргумент из двух обязательных.
        # Это вызовет TypeError внутри самой функции: multiply() missing 1 required positional argument: 'y'
        with pytest.raises(ValueError):
            await ToolRegistry.execute("multiply", {"x": 5})

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises(self):
        """Вызов незарегистрированного инструмента."""
        with pytest.raises(ValueError, match="Tool 'unknown' not found"):
            await ToolRegistry.execute("unknown", {})

    def test_get_openai_tools_schema(self):
        """Генерация JSON Schema для OpenAI API."""
        @ToolRegistry.register(description="Search docs")
        async def search_docs(query: str) -> list:
            return []

        schema = ToolRegistry.get_openai_tools_schema()
        assert any(tool["function"]["name"] == "search_docs" for tool in schema)